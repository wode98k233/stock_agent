"""JSON 策略编译器单元测试"""
import json
import pytest
from backtest.json_compiler import (
    compile_strategy, RuleNode, _CompileContext,
    _build_rule_tree, _make_empty_node,
)
from backtest.strategy_base import StockSolveStrategy


# ============================================================
# 测试辅助
# ============================================================

def _make_dual_ma_json_str():
    """双均线金叉策略 JSON"""
    return json.dumps({
        'meta': {
            'strategy_id': 'dual_ma',
            'name': '双均线金叉',
            'description': '测试策略',
            'category': 'trend',
            'tags': ['test'],
        },
        'params': {
            'fast': {'value': 5, 'min': 2, 'max': 120, 'label': '快线', 'type': 'int'},
            'slow': {'value': 20, 'min': 5, 'max': 250, 'label': '慢线', 'type': 'int'},
        },
        'conditions': {
            'buy': {
                'logic': 'AND',
                'rules': [{
                    'type': 'indicator',
                    'left': {'func': 'ma', 'args': ['close', '{fast}']},
                    'op': 'cross_above',
                    'right': {'func': 'ma', 'args': ['close', '{slow}']},
                }],
            },
            'sell': {
                'logic': 'AND',
                'rules': [{
                    'type': 'indicator',
                    'left': {'func': 'ma', 'args': ['close', '{fast}']},
                    'op': 'cross_below',
                    'right': {'func': 'ma', 'args': ['close', '{slow}']},
                }],
            },
        },
    })


def _make_rsi_json_str():
    """RSI 超买超卖策略 JSON"""
    return json.dumps({
        'meta': {
            'strategy_id': 'rsi_test',
            'name': 'RSI 测试',
            'tags': ['test'],
        },
        'params': {
            'period': {'value': 14, 'min': 2, 'max': 60, 'label': '周期', 'type': 'int'},
            'oversold': {'value': 30, 'min': 10, 'max': 40, 'label': '超卖线', 'type': 'int'},
        },
        'conditions': {
            'buy': {
                'logic': 'AND',
                'rules': [{
                    'type': 'indicator',
                    'left': {'func': 'rsi', 'args': ['close', '{period}']},
                    'op': '<',
                    'right': {'value': 30},
                }],
            },
            'sell': {
                'logic': 'AND',
                'rules': [],
            },
        },
    })


# ============================================================
# compile_strategy 基础测试
# ============================================================


class TestCompileStrategy:
    """编译入口测试"""

    def test_compile_dual_ma_returns_class(self):
        """编译双均线 JSON 返回策略类"""
        cls = compile_strategy(_make_dual_ma_json_str())
        assert issubclass(cls, StockSolveStrategy)
        assert cls.__name__ == 'dual_ma'

    def test_compile_rsi_returns_class(self):
        """编译 RSI JSON 返回策略类"""
        cls = compile_strategy(_make_rsi_json_str())
        assert issubclass(cls, StockSolveStrategy)
        assert cls.__name__ == 'rsi_test'

    def test_compile_invalid_json_raises(self):
        """无效 JSON 抛出异常"""
        with pytest.raises((json.JSONDecodeError, ValueError)):
            compile_strategy('not json')

    def test_compile_empty_params(self):
        """空 params 也能编译"""
        json_str = json.dumps({
            'meta': {'name': 'empty'},
            'params': {},
            'conditions': {
                'buy': {'logic': 'AND', 'rules': []},
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        cls = compile_strategy(json_str)
        assert issubclass(cls, StockSolveStrategy)

    def test_compile_unknown_indicator_raises(self):
        """未知指标函数编译时直接抛出 ValueError"""
        json_str = json.dumps({
            'meta': {'name': 'bad'},
            'params': {'x': {'value': 1}},
            'conditions': {
                'buy': {
                    'logic': 'AND',
                    'rules': [{
                        'type': 'indicator',
                        'left': {'func': 'nonexistent_func', 'args': ['close']},
                        'op': '>',
                        'right': {'value': 0},
                    }],
                },
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        # 行为变更：未知函数在编译时直接抛出 ValueError
        with pytest.raises(ValueError, match="未知指标函数"):
            compile_strategy(json_str)


# ============================================================
# 参数测试
# ============================================================


class TestParams:
    """params 解析测试"""

    def test_params_tuple_contains_strategy_params(self):
        """params tuple 包含策略定义参数"""
        cls = compile_strategy(_make_dual_ma_json_str())
        param_items = dict(cls.params._getitems())
        assert 'fast' in param_items
        assert 'slow' in param_items
        assert param_items['fast'] == 5
        assert param_items['slow'] == 20

    def test_params_inherits_base_params(self):
        """继承基类 params（stop_loss, take_profit 等）"""
        cls = compile_strategy(_make_dual_ma_json_str())
        param_items = dict(cls.params._getitems())
        assert 'stop_loss' in param_items  # StockSolveStrategy 基类参数
        assert 'take_profit' in param_items
        assert 't_plus_1' in param_items

    def test_get_params_info_returns_schema(self):
        """get_params_info 返回前端表单 schema"""
        cls = compile_strategy(_make_dual_ma_json_str())
        schema = cls.get_params_info()
        assert isinstance(schema, list)
        assert len(schema) == 2
        keys = [p['key'] for p in schema]
        assert 'fast' in keys
        assert 'slow' in keys
        for item in schema:
            assert 'key' in item
            assert 'label' in item
            assert 'type' in item
            assert 'default' in item


class TestChartIndicators:
    """图表指标测试"""

    def test_get_chart_indicators_resolves_params(self):
        """get_chart_indicators 解析 {param} 引用"""
        json_str = json.dumps({
            'meta': {'name': 'test'},
            'params': {
                'fast': {'value': 5},
                'slow': {'value': 20},
            },
            'conditions': {
                'buy': {'logic': 'AND', 'rules': []},
                'sell': {'logic': 'AND', 'rules': []},
            },
            'chart_indicators': [
                {'type': 'MA', 'period': '{fast}', 'panel': 'main'},
            ],
        })
        cls = compile_strategy(json_str)
        result = cls.get_chart_indicators({'fast': 5})
        assert result[0]['period'] == 5

    def test_get_chart_indicators_empty_by_default(self):
        """默认返回空列表"""
        cls = compile_strategy(_make_dual_ma_json_str())
        result = cls.get_chart_indicators({})
        assert isinstance(result, list)


# ============================================================
# 规则树测试
# ============================================================


class TestRuleTree:
    """规则树构建测试"""

    def test_build_single_and_rule(self):
        """单条 AND 规则 → 1个叶子节点"""
        ctx = _CompileContext()
        rules = [{
            'type': 'indicator',
            'left': {'func': 'rsi', 'args': ['close', 14]},
            'op': '<',
            'right': {'value': 30},
        }]
        tree = _build_rule_tree(rules, 'AND', ctx)
        assert tree.logic == 'AND'
        assert len(tree.rules) == 1
        assert tree.rules[0].is_leaf
        assert tree.rules[0].op == '<'

    def test_build_two_rules_with_and(self):
        """两条 AND 规则 → 2个叶子节点"""
        ctx = _CompileContext()
        rules = [
            {'type': 'indicator', 'left': {'func': 'rsi', 'args': ['close', 14]}, 'op': '<', 'right': {'value': 30}},
            {'type': 'indicator', 'left': {'data': 'volume'}, 'op': '>', 'right': {'value': 1000000}},
        ]
        tree = _build_rule_tree(rules, 'AND', ctx)
        assert len(tree.rules) == 2
        assert all(r.is_leaf for r in tree.rules)

    def test_build_nested_group(self):
        """嵌套 group: AND(leaf1, OR(leaf2, leaf3))"""
        ctx = _CompileContext()
        rules = [
            {'type': 'indicator', 'left': {'func': 'ma', 'args': ['close', 5]}, 'op': '>', 'right': {'value': 10}},
            {
                'type': 'group', 'logic': 'OR', 'rules': [
                    {'type': 'indicator', 'left': {'func': 'rsi', 'args': ['close', 14]}, 'op': '<', 'right': {'value': 30}},
                    {'type': 'indicator', 'left': {'data': 'volume'}, 'op': '>', 'right': {'value': 2000000}},
                ],
            },
        ]
        tree = _build_rule_tree(rules, 'AND', ctx)
        assert tree.logic == 'AND'
        assert len(tree.rules) == 2
        # 第一个是叶子
        assert tree.rules[0].is_leaf
        # 第二个是 group
        assert tree.rules[1].logic == 'OR'
        assert len(tree.rules[1].rules) == 2
        assert tree.rules[1].rules[0].is_leaf

    def test_empty_rules_returns_false_node(self):
        """空规则返回永不触发的节点"""
        ctx = _CompileContext()
        tree = _build_rule_tree([], 'AND', ctx)
        assert tree.is_leaf
        assert tree.op == 'empty'

    def test_cross_above_registers_cross_entry(self):
        """cross_above 注册 CrossOver 条目"""
        ctx = _CompileContext()
        ctx.param_names = ['fast', 'slow']
        rules = [{
            'type': 'indicator',
            'left': {'func': 'ma', 'args': ['close', '{fast}']},
            'op': 'cross_above',
            'right': {'func': 'ma', 'args': ['close', '{slow}']},
        }]
        _build_rule_tree(rules, 'AND', ctx)
        assert len(ctx.cross_entries) == 1
        left, right = ctx.cross_entries[0]
        assert left.startswith('ind_')
        assert right.startswith('ind_')

    def test_cross_below_registers_cross_entry(self):
        """cross_below 注册 CrossOver 条目"""
        ctx = _CompileContext()
        ctx.param_names = ['fast', 'slow']
        rules = [{
            'type': 'indicator',
            'left': {'func': 'ema', 'args': ['close', '{fast}']},
            'op': 'cross_below',
            'right': {'func': 'ema', 'args': ['close', '{slow}']},
        }]
        _build_rule_tree(rules, 'AND', ctx)
        assert len(ctx.cross_entries) == 1


# ============================================================
# 指标收集测试
# ============================================================


class TestIndicatorCollection:
    """指标去重收集测试"""

    def test_same_indicator_deduped(self):
        """相同的 (func, args) 去重"""
        json_str = json.dumps({
            'meta': {'name': 'test'},
            'params': {
                'fast': {'value': 5},
                'slow': {'value': 20},
            },
            'conditions': {
                'buy': {
                    'logic': 'AND',
                    'rules': [
                        {
                            'type': 'indicator',
                            'left': {'func': 'ma', 'args': ['close', '{fast}']},
                            'op': '>',
                            'right': {'func': 'ma', 'args': ['close', '{slow}']},
                        },
                    ],
                },
                'sell': {
                    'logic': 'AND',
                    'rules': [
                        {
                            'type': 'indicator',
                            # 与 buy 相同的指标组合
                            'left': {'func': 'ma', 'args': ['close', '{fast}']},
                            'op': '<',
                            'right': {'func': 'ma', 'args': ['close', '{slow}']},
                        },
                    ],
                },
            },
        })
        cls = compile_strategy(json_str)
        assert issubclass(cls, StockSolveStrategy)

    def test_macd_with_field(self):
        """MACD 带 field 提取"""
        json_str = json.dumps({
            'meta': {'name': 'macd_test'},
            'params': {
                'fast': {'value': 12},
                'slow': {'value': 26},
                'signal': {'value': 9},
            },
            'conditions': {
                'buy': {
                    'logic': 'AND',
                    'rules': [{
                        'type': 'indicator',
                        'left': {'func': 'macd', 'args': ['close', '{fast}', '{slow}', '{signal}'], 'field': 'macd'},
                        'op': 'cross_above',
                        'right': {'func': 'macd', 'args': ['close', '{fast}', '{slow}', '{signal}'], 'field': 'signal'},
                    }],
                },
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        cls = compile_strategy(json_str)
        assert issubclass(cls, StockSolveStrategy)

    def test_boll_with_field(self):
        """布林带带 field 提取"""
        json_str = json.dumps({
            'meta': {'name': 'boll_test'},
            'params': {'period': {'value': 20}, 'dev': {'value': 2}},
            'conditions': {
                'buy': {
                    'logic': 'AND',
                    'rules': [{
                        'type': 'indicator',
                        'left': {'data': 'close'},
                        'op': '>',
                        'right': {'func': 'boll', 'args': ['close', '{period}', '{dev}'], 'field': 'upper'},
                    }],
                },
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        cls = compile_strategy(json_str)
        assert issubclass(cls, StockSolveStrategy)


# ============================================================
# 数据引用测试
# ============================================================


class TestDataRefs:
    """数据引用解析测试"""

    def test_close_data_ref(self):
        """close 数据引用"""
        json_str = json.dumps({
            'meta': {'name': 'data_test'},
            'params': {},
            'conditions': {
                'buy': {
                    'logic': 'AND',
                    'rules': [{
                        'type': 'indicator',
                        'left': {'data': 'close'},
                        'op': '>',
                        'right': {'value': 10},
                    }],
                },
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        cls = compile_strategy(json_str)
        assert issubclass(cls, StockSolveStrategy)

    def test_volume_data_ref(self):
        """volume 数据引用"""
        json_str = json.dumps({
            'meta': {'name': 'vol_test'},
            'params': {},
            'conditions': {
                'buy': {
                    'logic': 'AND',
                    'rules': [{
                        'type': 'indicator',
                        'left': {'data': 'volume'},
                        'op': '>',
                        'right': {'value': 1000000},
                    }],
                },
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        cls = compile_strategy(json_str)
        assert issubclass(cls, StockSolveStrategy)


# ============================================================
# 运算符测试
# ============================================================


class TestOperators:
    """各种运算符支持测试"""

    @pytest.mark.parametrize('op', ['>', '<', '>=', '<=', '==', '!='])
    def test_compare_operators_compile(self, op):
        """所有比较运算符都能编译"""
        json_str = json.dumps({
            'meta': {'name': 'op_test'},
            'params': {'n': {'value': 14}},
            'conditions': {
                'buy': {
                    'logic': 'AND',
                    'rules': [{
                        'type': 'indicator',
                        'left': {'func': 'rsi', 'args': ['close', '{n}']},
                        'op': op,
                        'right': {'value': 50},
                    }],
                },
                'sell': {'logic': 'AND', 'rules': []},
            },
        })
        cls = compile_strategy(json_str)
        assert issubclass(cls, StockSolveStrategy)


# ============================================================
# RuleNode 单元测试
# ============================================================


class TestRuleNodeClass:
    """RuleNode 类行为测试"""

    def test_new_node_is_leaf(self):
        node = RuleNode()
        assert node.is_leaf

    def test_node_with_logic_is_not_leaf(self):
        node = RuleNode()
        node.logic = 'AND'
        assert not node.is_leaf

    def test_node_with_rules_not_leaf(self):
        node = RuleNode()
        node.logic = 'AND'
        node.rules.append(RuleNode())
        assert not node.is_leaf


# ============================================================
# 4 个内置模板完整编译测试
# ============================================================


class TestBuiltinTemplates:
    """验证 4 个内置 JSON 模板都能通过编译"""

    TEMPLATES_DIR = 'backtest/strategies/json_templates'

    @pytest.mark.parametrize('template_name', [
        'dual_ma_crossover', 'macd_crossover', 'rsi_overbought_oversold', 'boll_breakout',
    ])
    def test_builtin_template_compiles(self, template_name):
        """内置模板编译成功"""
        import os
        path = os.path.join(self.TEMPLATES_DIR, f'{template_name}.json')
        with open(path, encoding='utf-8') as f:
            cls = compile_strategy(f.read())
        assert issubclass(cls, StockSolveStrategy)
        # 验证 params_schema 可用
        schema = cls.get_params_info()
        assert len(schema) > 0

    def test_dual_ma_has_correct_params(self):
        """双均线模板参数正确"""
        import os
        path = os.path.join(self.TEMPLATES_DIR, 'dual_ma_crossover.json')
        with open(path, encoding='utf-8') as f:
            cls = compile_strategy(f.read())
        param_items = dict(cls.params._getitems())
        assert param_items['fast_period'] == 5
        assert param_items['slow_period'] == 20

    def test_rsi_has_correct_params(self):
        """RSI 模板参数正确"""
        import os
        path = os.path.join(self.TEMPLATES_DIR, 'rsi_overbought_oversold.json')
        with open(path, encoding='utf-8') as f:
            cls = compile_strategy(f.read())
        param_items = dict(cls.params._getitems())
        assert param_items['rsi_period'] == 14
        assert param_items['oversold'] == 30
        assert param_items['overbought'] == 70
