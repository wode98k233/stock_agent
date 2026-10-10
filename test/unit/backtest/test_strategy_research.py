"""A3 策略研发闭环 单测（AC-2/3/4/5/6/8/10 + Spike 五条修订）

覆盖：
- 状态机全状态转移（GEN→COMPILE→BACKTEST→EVAL→DECIDE、重试、failed、终止、取消）
- export_llm_schema 输出含全部 12 指标与合法 field
- 编译期 field 校验（合法通过 / 非法 field 拒绝）
- win_rate 脏值防护（3.0 → None）
- OOS 短样本保护（trades<5 不参与最优比较）
- 红线（IS/OOS 夏普比 >1.3 不得当选最优）
- 终止条件（converged / max_rounds / budget）
- 两表 DDL 幂等 + CRUD 往返
- 敏感词扫描

LLM 与回测全部 mock，不外呼。
"""
import json
import os
import sys

import pytest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest.strategy_research import (
    CONVERGED_STREAK,
    DISCLAIMER,
    IsoOsRunner,
    IterRecord,
    MAX_ROUNDS,
    OOS_MIN_TRADES,
    STOP_BUDGET,
    STOP_CONVERGED,
    STOP_FAILED,
    STOP_MAX_ROUNDS,
    SENSITIVE_WORDS,
    BacktestRunStore,
    StrategyResearchLoop,
    StrategyGenerator,
    clamp_win_rate,
    diff_strategy,
    extract_json_block,
    sanitize_text,
    scan_sensitive_words,
    strategy_hash,
)


# ============================================================
# 替身（Fake Generator / Runner / Store）
# ============================================================


def _ok_gen(hypothesis='假设', name='测试策略'):
    """构造合法策略 JSON（真实可编译：MA 金叉 + 放量）。"""
    return {
        'meta': {'name': name, 'description': hypothesis, 'tags': [], 'source': 'agent'},
        'params': {
            'fast': {'value': 5, 'min': 2, 'max': 60, 'step': 1, 'label': '快线', 'type': 'int'},
            'slow': {'value': 20, 'min': 5, 'max': 200, 'step': 1, 'label': '慢线', 'type': 'int'},
        },
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator',
                 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_above',
                 'right': {'func': 'ma', 'args': ['close', '{slow}']}},
            ]},
            'sell': {'logic': 'AND', 'rules': [
                {'type': 'indicator',
                 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_below',
                 'right': {'func': 'ma', 'args': ['close', '{slow}']}},
            ]},
        },
        'risk': {'stop_loss': {'type': 'fixed', 'value': 0.05},
                 'take_profit': {'type': 'fixed', 'value': 0.15}},
    }


class FakeGenerator:
    """脚本化生成器：按队列依次吐结果。"""

    def __init__(self, results):
        # results: list[GenResult | Exception]，耗尽后重复最后一项
        self.results = list(results)
        self.calls = []

    def generate_initial(self, idea, universe):
        self.calls.append(('initial', idea, universe))
        return self._next()

    def mutate(self, idea, history):
        self.calls.append(('mutate', len(history)))
        return self._next()

    def _next(self):
        r = self.results.pop(0) if self.results else self._last
        self._last = r
        if isinstance(r, Exception):
            raise r
        return r

    _last = None


class FakeRunner:
    """脚本化回测执行器：按 (is_metrics, oos_metrics) 队列吐结果。"""

    def __init__(self, results):
        self.results = list(results)

    def run_both(self, strategy_json, universe, period):
        r = self.results.pop(0) if self.results else self._last
        self._last = r
        if isinstance(r, Exception):
            raise r
        return r

    _last = None


def _metrics(sharpe, cagr=0.1, mdd=0.2, win_rate=0.5, trades=20):
    return {'cagr': cagr, 'sharpe': sharpe, 'mdd': mdd, 'win_rate': win_rate, 'trades': trades}


def _gen_result(sharpe_is=1.0, sharpe_oos=1.0, hypothesis='假设', trades_oos=20,
                name='测试策略'):
    from backtest.strategy_research import GenResult
    strategy = _ok_gen(name=name)
    strategy['meta']['name'] = f'{name}_{sharpe_oos}'
    return GenResult(
        ok=True, strategy_json=json.dumps(strategy, ensure_ascii=False),
        hypothesis=hypothesis, model='fake-model',
    ), _metrics(sharpe_is, trades=25), _metrics(sharpe_oos, trades=trades_oos)


class FakeStore:
    """内存版落库适配（无需真实 DB）。"""

    def __init__(self, run=None):
        self.runs = {run['run_uuid']: dict(run)} if run else {}
        self.iters = []
        self._next_id = 1

    def create_run(self, run_uuid, idea, universe, period, source='panel', dialog_uuid=None):
        self.runs[run_uuid] = {'run_uuid': run_uuid, 'idea': idea, 'universe': universe,
                               'period': period, 'status': 'running', 'source': source,
                               'dialog_uuid': dialog_uuid, 'best_iter_id': None,
                               'total_iters': 0}

    def update_run(self, run_uuid, **kw):
        self.runs[run_uuid].update(kw)

    def get_run(self, run_uuid):
        return self.runs.get(run_uuid)

    def insert_iter(self, run_uuid, record):
        record.id = self._next_id
        self._next_id += 1
        self.iters.append(record)
        return record.id

    def set_iter_verdict(self, iter_id, verdict):
        for it in self.iters:
            if it.id == iter_id:
                it.verdict = verdict

    def set_generator_context(self, idea, universe, period):
        # FakeStore 同时充当 loop 的 idea/universe/period 来源
        pass


def _make_run(store, run_uuid='SR-TEST'):
    store.create_run(run_uuid, idea='测试想法', universe='600519',
                     period='2023-01-01~2026-01-01')
    return store


def _loop(store, generator, runner, **kw):
    return StrategyResearchLoop('SR-TEST', db=store, generator=generator,
                                runner=runner, **kw)


# ============================================================
# 1. 基础转移：首轮生成 → 回测 → 落库（happy path）
# ============================================================


class TestHappyPath:
    def test_first_round_flow(self):
        store = _make_run(FakeStore())
        g, is_m, oos_m = _gen_result(1.2, 1.0)
        lp = _loop(store, FakeGenerator([g]), FakeRunner([(is_m, oos_m)]),
                   budget=1)  # 预算 1：一轮后 budget 终止，聚焦单轮
        result = lp.run()

        assert result.total_iters == 1
        assert result.stop_reason == STOP_BUDGET
        assert store.iters[0].compile_ok == 1
        assert store.iters[0].verdict in ('keep', 'best')  # 唯一有效轮次会被选为 best
        assert store.iters[0].is_metrics == is_m
        assert store.iters[0].oos_metrics == oos_m
        assert store.iters[0].strategy_hash == strategy_hash(g.strategy_json)
        assert store.runs['SR-TEST']['status'] == 'done'

    def test_generate_initial_used_first_then_mutate(self):
        store = _make_run(FakeStore())
        g1, m1, m2 = _gen_result(1.0, 0.8, hypothesis='第一轮')
        g2, m3, m4 = _gen_result(1.0, 0.9, hypothesis='第二轮')
        gen = FakeGenerator([g1, g2])
        lp = _loop(store, gen, FakeRunner([(m1, m2), (m3, m4)]), budget=2)
        lp.run()
        assert gen.calls[0][0] == 'initial'
        assert gen.calls[1][0] == 'mutate'
        assert gen.calls[1][1] == 1  # 基于第 1 轮历史变异

    def test_progress_events_emitted(self):
        store = _make_run(FakeStore())
        g, m1, m2 = _gen_result(1.0, 1.0)
        events = []
        lp = _loop(store, FakeGenerator([g]), FakeRunner([(m1, m2)]),
                   budget=1, progress_cb=lambda e, d: events.append(e))
        lp.run()
        assert 'strategy_iter_start' in events
        assert 'strategy_iter_done' in events
        assert 'strategy_run_done' in events

    def test_backtest_failure_fails_task(self):
        store = _make_run(FakeStore())
        g, _, _ = _gen_result()
        lp = _loop(store, FakeGenerator([g]),
                   FakeRunner([RuntimeError('资金不足')]))
        result = lp.run()
        assert result.status == 'failed'
        assert result.stop_reason == STOP_FAILED
        assert '资金不足' in store.iters[0].compile_error


# ============================================================
# 2. 重试兜底（AC-2 / AC-3）
# ============================================================


class TestRetryPath:
    def test_real_generator_retries_with_error_feedback(self):
        """真实 StrategyGenerator：LLM 先吐非法指标策略 → 编译失败 → 反馈重试成功。"""
        good_obj = _ok_gen()
        bad_obj = _ok_gen(name='坏策略')
        bad_obj['conditions']['buy']['rules'][0]['left'] = {'func': 'obv', 'args': ['close']}

        responses = [
            json.dumps({'hypothesis': '尝试OBV', 'strategy': bad_obj}, ensure_ascii=False),
            json.dumps({'hypothesis': '改为MACD', 'strategy': good_obj}, ensure_ascii=False),
        ]

        class FakeLLM:
            def __init__(self):
                self.invoked = []

            def invoke(self, messages):
                self.invoked.append(messages)
                return type('R', (), {'content': responses[len(self.invoked) - 1],
                                      'response_metadata': {'model_name': 'fake'}})()

        with patch.object(StrategyGenerator, '_pacing', lambda self, extra=0.0: None):
            gen = StrategyGenerator(llm=FakeLLM(), pacing_seconds=0, backoff_seconds=0)
            result = gen.generate_initial('MACD 金叉', '600519')

        assert result.ok is True
        assert 'MACD' in result.hypothesis
        assert result.attempts == 2
        assert '未知指标函数' in result.error if result.error else True  # 无最终错误

    def test_three_failures_give_readable_error(self):
        """AC-3：连续 3 次非法 → failed + 可读原因（无堆栈）。"""
        bad_obj = _ok_gen(name='坏策略')
        bad_obj['conditions']['buy']['rules'][0]['left'] = {'func': 'obv', 'args': ['close']}
        raw = json.dumps({'hypothesis': 'x', 'strategy': bad_obj}, ensure_ascii=False)

        class FakeLLM:
            def invoke(self, messages):
                return type('R', (), {'content': raw, 'response_metadata': {}})()

        with patch.object(StrategyGenerator, '_pacing', lambda self, extra=0.0: None):
            gen = StrategyGenerator(llm=FakeLLM(), pacing_seconds=0, backoff_seconds=0)
            result = gen.generate_initial('想法', '600519')

        assert result.ok is False
        assert '未知指标函数' in result.error
        assert 'Traceback' not in result.error
        assert result.attempts == 3  # 初次 + 2 次重试

    def test_unparseable_json_retries_then_fails(self):
        class FakeLLM:
            def invoke(self, messages):
                return type('R', (), {'content': '这不是JSON', 'response_metadata': {}})()

        with patch.object(StrategyGenerator, '_pacing', lambda self, extra=0.0: None):
            gen = StrategyGenerator(llm=FakeLLM(), pacing_seconds=0, backoff_seconds=0)
            result = gen.generate_initial('想法', '600519')
        assert result.ok is False
        assert 'JSON' in result.error


# ============================================================
# 3. 红线与选优（AC-4 + Spike 修订 3）
# ============================================================


class TestRedLineAndBest:
    def _records(self, *spec):
        # spec: (sharpe_is, sharpe_oos, oos_trades) 元组序列
        recs = []
        for i, (s_is, s_oos, trades) in enumerate(spec, 1):
            g, _, _ = _gen_result(name=f'策略{i}')
            rec = IterRecord(round=i, compile_ok=1, hypothesis=f'第{i}轮',
                             strategy_json=g.strategy_json,
                             is_metrics=_metrics(s_is), oos_metrics=_metrics(s_oos, trades=trades))
            rec.overfit_flag = 1 if StrategyResearchLoop('x', None, None, None).mark_overfit(rec) else 0
            rec.low_sample = 1 if trades < OOS_MIN_TRADES else 0
            recs.append(rec)
        return recs

    def test_mark_overfit_ratio(self):
        """IS 1.8 / OOS 0.9 → 比值 2.0 > 1.3 → 过拟合（AC-4）。"""
        lp = StrategyResearchLoop('x', None, None, None)
        rec = IterRecord(round=1, compile_ok=1,
                         is_metrics=_metrics(1.8), oos_metrics=_metrics(0.9))
        assert lp.mark_overfit(rec) is True

    def test_mark_overfit_negative_oos(self):
        """OOS ≤0 且 IS>0 → 过拟合迹象。"""
        lp = StrategyResearchLoop('x', None, None, None)
        rec = IterRecord(round=1, compile_ok=1,
                         is_metrics=_metrics(0.5), oos_metrics=_metrics(-0.3))
        assert lp.mark_overfit(rec) is True

    def test_mark_overfit_both_negative(self):
        lp = StrategyResearchLoop('x', None, None, None)
        rec = IterRecord(round=1, compile_ok=1,
                         is_metrics=_metrics(-0.5), oos_metrics=_metrics(-0.3))
        assert lp.mark_overfit(rec) is False

    def test_overfit_round_not_selected(self):
        """AC-4：IS 虚高轮次（1.8/0.9）不得当选，最优旁落。"""
        lp = StrategyResearchLoop('x', None, None, None)
        recs = self._records((1.8, 0.9, 20), (0.8, 0.7, 20))
        best = lp.select_best(recs)
        assert best.round == 2  # 过拟合的第 1 轮被排除

    def test_low_sample_round_not_selected(self):
        """Spike 修订 3：OOS trades<5 的轮次不参与夏普比较。"""
        lp = StrategyResearchLoop('x', None, None, None)
        recs = self._records((1.0, 5.0, 3), (0.5, 0.6, 30))  # 第 1 轮夏普虚高但样本 3 笔
        best = lp.select_best(recs)
        assert best.round == 2

    def test_no_candidate_returns_none(self):
        lp = StrategyResearchLoop('x', None, None, None)
        recs = self._records((1.8, 0.9, 3))  # 唯一轮次既过拟合又低样本
        assert lp.select_best(recs) is None

    def test_step_marks_overfit_and_low_sample(self):
        store = _make_run(FakeStore())
        g, is_m, oos_m = _gen_result(2.0, -0.5, trades_oos=3)
        lp = _loop(store, FakeGenerator([g]), FakeRunner([(is_m, oos_m)]), budget=1)
        lp.run()
        it = store.iters[0]
        assert it.overfit_flag == 1
        assert it.low_sample == 1
        assert it.verdict == 'reject'


# ============================================================
# 4. 终止条件（AC-5 / AC-6 / 预算 / 取消）
# ============================================================


class TestTermination:
    def test_converged_stops(self):
        """连续 2 轮 OOS 夏普未提升 → converged（AC-5）。"""
        store = _make_run(FakeStore())
        gen, runner = FakeGenerator([]), FakeRunner([])
        specs = [(1.0, 1.0), (1.0, 1.5), (1.0, 1.2), (1.0, 1.3)]  # 后两轮未刷新 1.5
        for s_is, s_oos in specs:
            g, m1, m2 = _gen_result(s_is, s_oos)
            gen.results.append(g)
            runner.results.append((m1, m2))
        lp = _loop(store, gen, runner)
        result = lp.run()
        assert result.stop_reason == STOP_CONVERGED
        assert result.total_iters == 4
        # 最优是 OOS 夏普 1.5 的第 2 轮
        assert result.best_iter.round == 2

    def test_converged_ignores_low_sample(self):
        """短样本轮次不参与收敛判定与最优比较（Spike 修订 3）。"""
        store = _make_run(FakeStore())
        gen, runner = FakeGenerator([]), FakeRunner([])
        # 第 1 轮 1.5（参照）；第 2 轮夏普 9.9 但只有 2 笔（低样本，无效）；第 3、4 轮 1.2 未提升
        specs = [(1.0, 1.5, 20), (1.0, 9.9, 2), (1.0, 1.2, 20), (1.0, 1.2, 20)]
        for s_is, s_oos, trades in specs:
            g, m1, m2 = _gen_result(s_is, s_oos, trades_oos=trades)
            gen.results.append(g)
            runner.results.append((m1, m2))
        lp = _loop(store, gen, runner)
        result = lp.run()
        assert result.stop_reason == STOP_CONVERGED
        assert result.total_iters == 4
        # 低样本虚高轮（9.9/2笔）不得当选，最优仍是第 1 轮的 1.5
        assert result.best_iter.round == 1

    def test_max_rounds_hard_stop(self):
        """AC-6：第 8 轮后强制终止，不进入第 9 轮。"""
        store = _make_run(FakeStore())
        gen, runner = FakeGenerator([]), FakeRunner([])
        for i in range(MAX_ROUNDS + 3):
            g, m1, m2 = _gen_result(1.0, 1.0 + i * 0.1)  # 每轮都提升，只可能被轮数终止
            gen.results.append(g)
            runner.results.append((m1, m2))
        lp = _loop(store, gen, runner)
        result = lp.run()
        assert result.stop_reason == STOP_MAX_ROUNDS
        assert result.total_iters == MAX_ROUNDS

    def test_budget_stops(self):
        store = _make_run(FakeStore())
        g, m1, m2 = _gen_result(1.0, 1.0)
        lp = _loop(store, FakeGenerator([g]), FakeRunner([(m1, m2)]), budget=1)
        result = lp.run()
        assert result.stop_reason == STOP_BUDGET
        assert result.total_iters == 1

    def test_cancel_stops(self):
        store = _make_run(FakeStore())
        g, m1, m2 = _gen_result(1.0, 1.0)
        flags = iter([False, True])
        lp = _loop(store, FakeGenerator([g]), FakeRunner([(m1, m2)]),
                   cancel_check=lambda: next(flags))
        result = lp.run()
        assert result.status == 'cancelled'
        assert store.runs['SR-TEST']['status'] == 'cancelled'

    def test_failed_generator_fails_run(self):
        """生成器返回 ok=False → failed，stop_reason=failed（AC-3 状态机侧）。"""
        from backtest.strategy_research import GenResult
        store = _make_run(FakeStore())
        gen = FakeGenerator([GenResult(ok=False, strategy_json=None, hypothesis='',
                                       error='LLM 调用失败')])
        lp = _loop(store, gen, FakeRunner([]))
        result = lp.run()
        assert result.status == 'failed'
        assert result.stop_reason == STOP_FAILED
        assert store.iters[0].verdict == 'failed'
        assert 'LLM 调用失败' in store.iters[0].compile_error

    def test_best_verdict_written(self):
        store = _make_run(FakeStore())
        gen, runner = FakeGenerator([]), FakeRunner([])
        for s in (0.5, 1.2):
            g, m1, m2 = _gen_result(1.0, s)
            gen.results.append(g)
            runner.results.append((m1, m2))
        lp = _loop(store, gen, runner, budget=2)
        result = lp.run()
        best_rec = [it for it in store.iters if it.id == result.best_iter.id][0]
        assert best_rec.verdict == 'best'


# ============================================================
# 5. win_rate 脏值防护 / 敏感词 / hash / diff / extract
# ============================================================


class TestHygiene:
    def test_win_rate_clamp_dirty_value(self):
        """Spike 修订 4：win_rate=3.0 → None。"""
        out = clamp_win_rate({'win_rate': 3.0, 'sharpe': 1.0})
        assert out['win_rate'] is None
        assert out['sharpe'] == 1.0

    def test_win_rate_valid_kept(self):
        assert clamp_win_rate({'win_rate': 0.55})['win_rate'] == 0.55
        assert clamp_win_rate({'win_rate': None})['win_rate'] is None
        assert clamp_win_rate(None) is None

    def test_sensitive_words_scan(self):
        assert '稳赚' in scan_sensitive_words('这个策略稳赚不赔')
        assert '必胜' in scan_sensitive_words('必胜 formula')
        assert scan_sensitive_words('正常描述') == []
        assert '稳赚' in SENSITIVE_WORDS and '躺赢' in SENSITIVE_WORDS

    def test_sanitize_text_removes_words(self):
        assert sanitize_text('稳赚策略') == '策略'
        assert sanitize_text('必胜+躺赢') == '+'

    def test_disclaimer_constant(self):
        assert '历史表现不代表未来收益' in DISCLAIMER

    def test_strategy_hash_stable(self):
        a = json.dumps({'meta': {'name': 'x'}}, ensure_ascii=False)
        b = json.dumps({'meta': {'name': 'x'}}, sort_keys=True, ensure_ascii=False)
        assert strategy_hash(a) == strategy_hash(b)

    def test_extract_json_block(self):
        assert extract_json_block('```json\n{"a": 1}\n```') == {'a': 1}
        assert extract_json_block('前缀 {"a": {"b": 2}} 后缀') == {'a': {'b': 2}}
        assert extract_json_block('没有json') is None

    def test_diff_strategy_params(self):
        prev = json.dumps(_ok_gen(), ensure_ascii=False)
        new_obj = _ok_gen()
        new_obj['params']['fast']['value'] = 10
        new_json = json.dumps(new_obj, ensure_ascii=False)
        d = diff_strategy(prev, new_json)
        assert 'fast' in d and '5 → 10' in d

    def test_diff_first_round(self):
        assert diff_strategy(None, '{}') == '首轮生成'


# ============================================================
# 6. export_llm_schema 与编译期 field 校验（Spike 修订 1）
# ============================================================


class TestExportSchemaAndFieldValidation:
    def test_schema_contains_all_12_indicators(self):
        from backtest.json_compiler import export_llm_schema, INDICATOR_MAP
        schema = export_llm_schema()
        for func in INDICATOR_MAP:  # atr/boll/cyw/dma/dpo/ema/kdj/ma/macd/rsi/volume/wma
            assert f'- {func}' in schema, f'schema 缺指标 {func}'
        assert 'atr' in schema and 'boll' in schema and 'cyw' in schema
        assert 'dma' in schema and 'dpo' in schema  # 旧手写 prompt 缺的三个也在

    def test_schema_contains_fields_and_ops(self):
        from backtest.json_compiler import export_llm_schema
        schema = export_llm_schema()
        for field in ('macd', 'signal', 'upper', 'mid', 'lower', 'k', 'd', 'j',
                      'dpo', 'madpo', 'dif', 'ama', 'cyw'):
            assert field in schema
        for op in ('cross_above', 'cross_below', '>=', '<='):
            assert op in schema

    def test_compile_rejects_invalid_field(self):
        """编译期 field 校验：kdj 带非法 field → ValueError 且带合法清单。"""
        from backtest.json_compiler import compile_strategy
        obj = _ok_gen()
        obj['conditions']['buy']['rules'][0]['left'] = {
            'func': 'kdj', 'args': ['high', 'low', 'close', '{fast}'], 'field': 'x'}
        with pytest.raises(ValueError, match='kdj'):
            compile_strategy(json.dumps(obj, ensure_ascii=False))

    def test_compile_accepts_valid_field(self):
        from backtest.json_compiler import compile_strategy
        obj = _ok_gen()
        obj['conditions']['buy']['rules'][0]['left'] = {
            'func': 'kdj', 'args': ['high', 'low', 'close', '{fast}'], 'field': 'k'}
        cls = compile_strategy(json.dumps(obj, ensure_ascii=False))
        assert cls is not None

    def test_compile_rejects_field_on_single_line_indicator(self):
        from backtest.json_compiler import compile_strategy
        obj = _ok_gen()
        obj['conditions']['buy']['rules'][0]['left'] = {
            'func': 'ma', 'args': ['close', '{fast}'], 'field': 'dpo'}
        with pytest.raises(ValueError, match='field'):
            compile_strategy(json.dumps(obj, ensure_ascii=False))


# ============================================================
# 7. IsoOsRunner 切分（Spike 修订 5）
# ============================================================


class TestSplitPeriod:
    def _runner_with_days(self, days):
        r = IsoOsRunner(split_ratio=0.7)
        r._trading_days = lambda universe, start, end: list(days)
        return r

    def test_oos_starts_next_trading_day(self):
        """OOS 起点取切分日次一交易日，不与 IS 尾日重复。"""
        days = [f'2024-01-{d:02d}' for d in range(1, 11)]  # 10 个交易日
        r = self._runner_with_days(days)
        (is_s, is_e), (oos_s, oos_e) = r.split_period(days[0], days[-1], '600519')
        assert is_s == days[0]
        assert is_e == days[6]           # int(10*0.7)=7 → 第 7 个交易日
        assert oos_s == days[7]          # 次一交易日（Spike 修订 5）
        assert oos_e == days[-1]

    def test_split_fallback_without_data(self):
        r = IsoOsRunner(split_ratio=0.7)
        r._trading_days = lambda *a, **k: []
        (is_s, is_e), (oos_s, oos_e) = r.split_period('2024-01-01', '2024-03-01', '600519')
        assert is_s == '2024-01-01' and oos_e == '2024-03-01'
        assert is_e <= oos_s

    def test_run_both_uses_cache(self):
        r = IsoOsRunner()
        calls = []
        r._trading_days = lambda *a, **k: ['2024-01-01'] * 0 or []
        r.split_period = lambda s, e, u, ratio=None: (('a', 'b'), ('c', 'd'))

        def fake_segment(sid, universe, s, e, label):
            calls.append(label)
            return {'cagr': 0.1, 'sharpe': 1.0, 'mdd': 0.1, 'win_rate': 0.5, 'trades': 10}

        r._run_segment = fake_segment
        sj = json.dumps(_ok_gen(), ensure_ascii=False)
        r.run_both(sj, '600519', ('a', 'd'))
        r.run_both(sj, '600519', ('a', 'd'))
        assert calls == ['IS', 'OOS']  # 第二次命中缓存
        is_m, oos_m = r.run_both(sj, '600519', ('a', 'd'))
        assert is_m['win_rate'] == 0.5  # clamp 后保持合法值


# ============================================================
# 8. DDL 幂等与 CRUD 往返（真实 SQLite，tmp 库）
# ============================================================


@pytest.fixture()
def sr_db(tmp_path):
    test_db = str(tmp_path / 'backtest.db')
    with patch('config.Config.get_backtest_db_path', return_value=test_db):
        from utils.cache.backtest_db import init_backtest_tables
        init_backtest_tables()
        yield test_db


class TestDDLAndCRUD:
    def test_tables_created(self, sr_db):
        import sqlite3
        conn = sqlite3.connect(sr_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert 'strategy_run' in tables
        assert 'strategy_iter' in tables

    def test_ddl_idempotent(self, sr_db):
        """AC-10 附属：重复 init 不报错、不清数据。"""
        from utils.cache.backtest_db import init_backtest_tables, create_strategy_run
        create_strategy_run('SR-IDEM', '想法', '600519', 'a~b')
        init_backtest_tables()
        init_backtest_tables()
        from utils.cache.backtest_db import get_strategy_run
        assert get_strategy_run('SR-IDEM') is not None

    def test_run_iter_roundtrip(self, sr_db):
        from utils.cache.backtest_db import (
            create_strategy_run, insert_strategy_iter, get_strategy_iters,
            update_strategy_run, get_strategy_run, update_strategy_iter,
            get_strategy_iter_by_id,
        )
        create_strategy_run('SR-RT', 'DPO 上穿 MADPO', '600519', '2023-01-01~2026-01-01',
                            source='panel', dialog_uuid='dlg-1')
        it_id = insert_strategy_iter(
            'SR-RT', round_no=1, hypothesis='假设A',
            strategy_json='{"meta": {}}', strategy_hash='h1',
            strategy_diff='首轮生成', compile_ok=1, compile_error=None,
            is_metrics={'cagr': 0.1, 'sharpe': 1.0, 'mdd': 0.2, 'win_rate': 0.55, 'trades': 20},
            oos_metrics={'cagr': 0.05, 'sharpe': 0.8, 'mdd': 0.15, 'win_rate': 0.5, 'trades': 8},
            overfit_flag=0, verdict='keep', model='fake')
        assert it_id > 0

        iters = get_strategy_iters('SR-RT')
        assert len(iters) == 1
        assert iters[0]['oos_metrics']['trades'] == 8  # JSON 已反序列化
        assert iters[0]['round'] == 1

        update_strategy_run('SR-RT', status='done', stop_reason='converged',
                            best_iter_id=it_id, total_iters=1)
        run = get_strategy_run('SR-RT')
        assert run['status'] == 'done' and run['best_iter_id'] == it_id
        assert run['dialog_uuid'] == 'dlg-1'

        update_strategy_iter(it_id, verdict='best')
        assert get_strategy_iter_by_id(it_id)['verdict'] == 'best'

    def test_insert_iter_unknown_run_raises(self, sr_db):
        from utils.cache.backtest_db import insert_strategy_iter
        with pytest.raises(ValueError):
            insert_strategy_iter('SR-NOPE', round_no=1, hypothesis=None,
                                 strategy_json=None, strategy_hash=None,
                                 strategy_diff=None, compile_ok=0, compile_error=None,
                                 is_metrics=None, oos_metrics=None)

    def test_list_runs_join_best(self, sr_db):
        from utils.cache.backtest_db import (
            create_strategy_run, insert_strategy_iter, update_strategy_run,
            list_strategy_runs, get_strategy_iters,
        )
        create_strategy_run('SR-J', '想法', '600519', 'a~b')
        it_id = insert_strategy_iter('SR-J', round_no=1, hypothesis='h',
                                     strategy_json='{}', strategy_hash='h',
                                     strategy_diff='首轮生成', compile_ok=1,
                                     compile_error=None,
                                     is_metrics={'sharpe': 1.0},
                                     oos_metrics={'sharpe': 0.9, 'trades': 10},
                                     overfit_flag=0, verdict='keep', model='m')
        update_strategy_run('SR-J', best_iter_id=it_id, total_iters=1)
        runs = list_strategy_runs()
        assert len(runs) == 1
        assert runs[0]['best_oos_metrics']['sharpe'] == 0.9  # JOIN + 反序列化
        assert get_strategy_iters('SR-J')[0]['is_metrics']['sharpe'] == 1.0
