"""JSON 策略编译器

将声明式 JSON 策略定义编译为 Backtrader 动态策略类。

JSON 结构:
{
    "meta": {...},
    "params": {"fast": {"value": 5, "min": 2, ...}},
    "conditions": {
        "buy": {"logic": "AND", "rules": [...]},
        "sell": {"logic": "AND", "rules": [...]}
    },
    "chart_indicators": [...]
}

编译流程:
1. 解析 params → Backtrader params tuple + params_schema
2. 遍历 rules 收集指标 → 去重分配 ind_N 名称
3. 为 cross_above/cross_below 创建 CrossOver 条目
4. 构建规则树 (RuleNode)
5. type() 动态生成策略类
"""
import json
import re
import logging
from typing import Dict, Any, List, Optional, Tuple, Type

import backtrader as bt

from backtest.strategy_base import StockSolveStrategy
from backtest.dpo_indicator import DPO
from backtest.dma_indicator import DMA
from backtest.cyw_indicator import CYW

logger = logging.getLogger(__name__)

# ============================================================
# 数据结构
# ============================================================


class RuleNode:
    """条件规则树节点"""

    def __init__(self):
        self.logic: Optional[str] = None  # 'AND' | 'OR' | None(叶子)
        self.rules: List['RuleNode'] = []
        # 叶子节点字段
        self.op: Optional[str] = None
        self.left_ref: Optional[str] = None  # 左值引用键
        self.right_ref: Optional[str] = None  # 右值引用键
        self.is_cross: bool = False  # 是否 cross_above/cross_below

    @property
    def is_leaf(self) -> bool:
        return self.logic is None


class _CompileContext:
    """编译上下文，持有编译过程中的中间状态"""

    def __init__(self):
        # 指标注册: key -> {'func': ..., 'args': ..., 'field': ..., 'bt_indicator_cls': ...}
        self.indicators: Dict[str, Dict] = {}
        # 数据引用: key -> line 路径 (如 'close')
        self.data_refs: Dict[str, str] = {}
        # 字面量: key -> value
        self.literals: Dict[str, Any] = {}
        # CrossOver 条目: [(left_key, right_key)]
        self.cross_entries: List[Tuple[str, str]] = []
        # 参数名集合
        self.param_names: List[str] = []
        # 计数器
        self._ind_counter = 0
        self._val_counter = 0

    def add_indicator(self, func: str, args: tuple, field: Optional[str] = None) -> str:
        """注册一个指标，返回唯一 key"""
        key = (func, args, field or '')
        for existing_key, info in self.indicators.items():
            if info['_signature'] == key:
                return existing_key
        ind_key = f'ind_{self._ind_counter}'
        self._ind_counter += 1
        self.indicators[ind_key] = {
            '_signature': key,
            'func': func,
            'args': args,
            'field': field,
        }
        return ind_key

    def add_literal(self, value: Any) -> str:
        """注册一个字面量，返回唯一 key"""
        for existing_key, v in self.literals.items():
            if v == value:
                return existing_key
        key = f'val_{self._val_counter}'
        self._val_counter += 1
        self.literals[key] = value
        return key


# ============================================================
# 指标函数映射
# ============================================================

# func_name → (Backtrader 指标类, 构造函数参数有序列表)
# args 顺序对应 JSON args，构造时按关键字传入
INDICATOR_MAP: Dict[str, dict] = {
    'ma': {
        'cls': bt.indicators.SMA,
        'arg_names': ['data', 'period'],
    },
    'ema': {
        'cls': bt.indicators.EMA,
        'arg_names': ['data', 'period'],
    },
    'wma': {
        'cls': bt.indicators.WMA,
        'arg_names': ['data', 'period'],
    },
    'rsi': {
        'cls': bt.indicators.RSI,
        'arg_names': ['data', 'period'],
    },
    'macd': {
        'cls': bt.indicators.MACD,
        'arg_names': ['data', 'period_me1', 'period_me2', 'period_signal'],
    },
    'kdj': {
        'cls': bt.indicators.Stochastic,
        'arg_names': ['data', 'period', 'period_dfast'],
        'fixed_kwargs': {'period_dfast': 3},
    },
    'boll': {
        'cls': bt.indicators.BollingerBands,
        'arg_names': ['data', 'period', 'devfactor'],
    },
    'atr': {
        'cls': bt.indicators.ATR,
        'arg_names': ['data', 'period'],
    },
    'volume': {
        'cls': None,  # 特殊处理：直接映射到 self.data.volume
        'arg_names': [],
    },
    'dpo': {
        'cls': DPO,
        'arg_names': ['data', 'period', 'smooth'],
    },
    'dma': {
        'cls': DMA,
        'arg_names': ['data', 'short', 'long', 'smooth'],
    },
    'cyw': {
        'cls': CYW,
        'arg_names': ['data', 'period'],
    },
}

# 数据名称 → self.data.xxx 的属性路径
DATA_REF_MAP: Dict[str, str] = {
    'close': 'close',
    'open': 'open',
    'high': 'high',
    'low': 'low',
    'volume': 'volume',
    'amount': 'amount',
    'pre_close': 'pre_close',
    'turnover_rate': 'turnover_rate',
    'pct_change': 'pct_change',
    'change_amount': 'change_amount',
    'amplitude': 'amplitude',
}

# MACD / BollingerBands 等多线指标的字段 → 属性名
FIELD_MAP: Dict[str, str] = {
    'macd': 'macd',
    'signal': 'signal',
    'upper': 'top',
    'mid': 'mid',
    'lower': 'bot',
    'k': 'percK',
    'd': 'percD',
    'j': None,  # KDJ 的 J 线需要计算：3K - 2D
    'dpo': 'dpo',
    'madpo': 'madpo',
    'dif': 'dif',
    'ama': 'ama',
    'cyw': 'cyw',
}


# ============================================================
# 运算符映射
# ============================================================

COMPARE_OPS = {'>', '<', '>=', '<=', '==', '!='}
CROSS_OPS = {'cross_above', 'cross_below'}

# 指标中文标签（供前端渲染下拉框）
INDICATOR_LABELS: Dict[str, str] = {
    'ma': '移动平均线 (MA)',
    'ema': '指数均线 (EMA)',
    'wma': '加权均线 (WMA)',
    'rsi': '相对强弱 (RSI)',
    'macd': 'MACD',
    'kdj': 'KDJ 随机指标',
    'boll': '布林带 (BOLL)',
    'atr': '平均真实波幅 (ATR)',
    'volume': '成交量',
    'dpo': '区间震荡线 (DPO)',
    'dma': '平行线差 (DMA)',
    'cyw': '主力控盘 (CYW)',
}

# 字段中文标签
FIELD_LABELS: Dict[str, str] = {
    'macd': 'DIF 线',
    'signal': 'DEA 线',
    'upper': '上轨',
    'mid': '中轨',
    'lower': '下轨',
    'k': 'K 线',
    'd': 'D 线',
    'j': 'J 线',
    'dpo': 'DPO 线',
    'madpo': 'MADPO 线',
    'dif': 'DIF 线',
    'ama': 'AMA 线',
    'cyw': 'CYW 线',
}

# 运算符中文标签
OPERATOR_LABELS: Dict[str, str] = {
    '>': '大于',
    '<': '小于',
    '>=': '大于等于',
    '<=': '小于等于',
    '==': '等于',
    '!=': '不等于',
    'cross_above': '上穿（金叉）',
    'cross_below': '下穿（死叉）',
}

# data ref 中文标签
DATA_REF_LABELS: Dict[str, str] = {
    'close': '收盘价',
    'open': '开盘价',
    'high': '最高价',
    'low': '最低价',
    'volume': '成交量',
    'amount': '成交额',
    'pre_close': '前收盘价',
    'turnover_rate': '换手率',
    'pct_change': '涨跌幅',
    'change_amount': '涨跌额',
    'amplitude': '振幅',
}


def get_strategy_meta() -> dict:
    """导出策略元数据，供前端渲染可视化构造器"""
    indicators = {}
    for func_name, info in INDICATOR_MAP.items():
        indicators[func_name] = {
            'label': INDICATOR_LABELS.get(func_name, func_name),
            'args': info['arg_names'],
            'fields': _fields_for_func(func_name),
        }

    operators = [{'value': k, 'label': v} for k, v in OPERATOR_LABELS.items()]

    data_refs = [{'value': k, 'label': v} for k, v in DATA_REF_LABELS.items()]

    # 内置模板 ID
    try:
        import os as _os
        from utils.app_paths import get_app_dir as _get_app_dir
        tmpl_dir = _os.path.join(_get_app_dir(), 'backtest', 'strategies', 'json_templates')
        templates = []
        if _os.path.isdir(tmpl_dir):
            for fn in sorted(_os.listdir(tmpl_dir)):
                if fn.endswith('.json'):
                    sid = fn.replace('.json', '')
                    try:
                        with open(_os.path.join(tmpl_dir, fn), encoding='utf-8') as f:
                            data = json.loads(f.read())
                        name = data.get('meta', {}).get('name', sid)
                    except Exception:
                        name = sid
                    templates.append({'strategy_id': sid, 'name': name})
    except Exception:
        templates = []

    return {
        'indicators': indicators,
        'operators': operators,
        'data_refs': data_refs,
        'templates': templates,
        'conditions': {'buy': '买入条件', 'sell': '卖出条件'},
    }


def _fields_for_func(func_name: str) -> list:
    """返回指标的多字段列表（如 MACD 有 macd/signal）"""
    if func_name == 'macd':
        return [{'value': 'macd', 'label': FIELD_LABELS['macd']},
                {'value': 'signal', 'label': FIELD_LABELS['signal']}]
    if func_name == 'boll':
        return [{'value': 'upper', 'label': FIELD_LABELS['upper']},
                {'value': 'mid', 'label': FIELD_LABELS['mid']},
                {'value': 'lower', 'label': FIELD_LABELS['lower']}]
    if func_name == 'kdj':
        return [{'value': 'k', 'label': FIELD_LABELS['k']},
                {'value': 'd', 'label': FIELD_LABELS['d']},
                {'value': 'j', 'label': FIELD_LABELS['j']}]
    if func_name == 'dpo':
        return [{'value': 'dpo', 'label': FIELD_LABELS['dpo']},
                {'value': 'madpo', 'label': FIELD_LABELS['madpo']}]
    if func_name == 'dma':
        return [{'value': 'dif', 'label': FIELD_LABELS['dif']},
                {'value': 'ama', 'label': FIELD_LABELS['ama']}]
    if func_name == 'cyw':
        return [{'value': 'cyw', 'label': FIELD_LABELS['cyw']}]
    return []


# ============================================================
# 公共入口
# ============================================================


def compile_strategy(json_str: str) -> Type[StockSolveStrategy]:
    """将 JSON 策略定义编译为 Backtrader 策略类

    Args:
        json_str: 符合 strategy-schema 的 JSON 字符串

    Returns:
        StockSolveStrategy 的子类，可直接传给 cerebro.addstrategy()
    """
    data = json.loads(json_str)

    ctx = _CompileContext()

    # Step 1: 解析 params
    params_raw = data.get('params', {})
    bt_params = _parse_params(params_raw, ctx)

    # Step 2: 解析 conditions → 收集指标 + 构建规则树
    conditions = data.get('conditions', {})
    buy_rules = conditions.get('buy', {}).get('rules', [])
    sell_rules = conditions.get('sell', {}).get('rules', [])
    buy_logic = conditions.get('buy', {}).get('logic', 'AND')
    sell_logic = conditions.get('sell', {}).get('logic', 'AND')

    buy_tree = _build_rule_tree(buy_rules, buy_logic, ctx)
    sell_tree = _build_rule_tree(sell_rules, sell_logic, ctx)

    # Step 3: 生成类
    meta = data.get('meta', {})
    strategy_name = meta.get('name', 'DynamicStrategy')
    class_name = _to_class_name(meta.get('strategy_id', '') or strategy_name)

    chart_indicators = data.get('chart_indicators', [])

    strategy_cls = _build_strategy_class(
        ctx=ctx,
        bt_params=bt_params,
        class_name=class_name,
        strategy_name=strategy_name,
        buy_tree=buy_tree,
        sell_tree=sell_tree,
        params_raw=params_raw,
        chart_indicators=chart_indicators,
        meta_description=meta.get('description', ''),
    )

    return strategy_cls


# ============================================================
# Step 1: 解析 params
# ============================================================


def _parse_params(params_raw: Dict[str, Any], ctx: _CompileContext) -> tuple:
    """将 JSON params 转为 Backtrader params tuple"""
    bt_params_list = []
    ctx.param_names = list(params_raw.keys())

    for key, info in params_raw.items():
        if isinstance(info, dict):
            default = info.get('value', info.get('default', 0))
            bt_params_list.append((key, default))
        else:
            bt_params_list.append((key, info))

    # 加上基类 params（止损止盈仓位等，由引擎传入，此处只定义策略特有参数）
    return tuple(bt_params_list)


# ============================================================
# Step 2: 构建规则树
# ============================================================


def _build_rule_tree(rules: list, logic: str, ctx: _CompileContext) -> RuleNode:
    """递归构建规则树"""
    if not rules:
        return _make_empty_node()

    node = RuleNode()
    node.logic = logic

    for rule in rules:
        rule_type = rule.get('type', 'indicator')

        if rule_type == 'group':
            # 嵌套逻辑组
            child_logic = rule.get('logic', 'AND')
            child_rules = rule.get('rules', [])
            node.rules.append(_build_rule_tree(child_rules, child_logic, ctx))
        elif rule_type in ('indicator', 'price', 'volume', 'pattern', 'time', 'custom'):
            leaf = RuleNode()
            op = rule.get('op', '>')
            left_def = rule.get('left', {})
            right_def = rule.get('right', {})

            if op in CROSS_OPS:
                # cross_above / cross_below: 左右都是指标
                left_key = _resolve_indicator(left_def, ctx)
                right_key = _resolve_indicator(right_def, ctx)
                ctx.cross_entries.append((left_key, right_key))

                leaf.op = op
                leaf.left_ref = left_key
                leaf.right_ref = right_key
                leaf.is_cross = True
            else:
                # 比较运算符
                leaf.op = op
                leaf.left_ref = _resolve_value(left_def, ctx)
                leaf.right_ref = _resolve_value(right_def, ctx)
                leaf.is_cross = False

            node.rules.append(leaf)
        else:
            logger.warning(f'未知规则类型: {rule_type}，跳过')

    return node


def _make_empty_node() -> RuleNode:
    """创建空节点（永不触发）"""
    leaf = RuleNode()
    leaf.op = 'empty'
    leaf.left_ref = 'val_empty'
    leaf.right_ref = 'val_empty'
    return leaf


def _resolve_value(defn: dict, ctx: _CompileContext) -> str:
    """解析左值/右值定义，返回引用 key"""
    if 'value' in defn:
        raw = defn['value']
        # {param} 引用走运行时参数解析
        if isinstance(raw, str) and raw.startswith('{') and raw.endswith('}'):
            param_name = raw[1:-1]
            if param_name in ctx.param_names:
                return f"param:{param_name}"
        return ctx.add_literal(raw)
    if 'func' in defn:
        return _resolve_indicator(defn, ctx)
    if 'data' in defn:
        return _resolve_data_ref(defn, ctx)
    if 'pattern' in defn:
        # 蜡烛形态暂不支持，返回字面量 False
        logger.warning(f"蜡烛形态暂不支持: {defn['pattern']}")
        return ctx.add_literal(False)
    if 'param' in defn:
        # 参数引用在运行时替换
        return f"param:{defn['param']}"
    # 默认字面量
    return ctx.add_literal(0)


def _resolve_indicator(defn: dict, ctx: _CompileContext) -> str:
    """解析指标函数定义，返回指标 key"""
    func = defn.get('func', 'ma')
    raw_args = defn.get('args', ['close'])
    field = defn.get('field')

    # 验证 func 是否为支持的指标函数
    if func not in INDICATOR_MAP:
        # 检查是否误用了数据字段作为指标函数
        if func in DATA_REF_MAP:
            raise ValueError(
                f"未知指标函数: '{func}'。\n"
                f"提示: '{func}' 是数据源字段，不是指标函数。\n"
                f"如需使用价格数据，请使用 'type': 'price' 规则，或将 '{func}' 作为指标的 args 参数。\n"
                f"支持的指标函数: {list(INDICATOR_MAP.keys())}"
            )
        else:
            raise ValueError(
                f"未知指标函数: '{func}'。\n"
                f"支持的指标函数: {list(INDICATOR_MAP.keys())}"
            )

    # 解析 args 中的 {param} 引用
    resolved_args = []
    for arg in raw_args:
        if isinstance(arg, str) and arg.startswith('{') and arg.endswith('}'):
            param_name = arg[1:-1]
            # 检查是否是已知参数名
            if param_name in ctx.param_names:
                resolved_args.append(('param', param_name))
            else:
                # 可能是数据引用
                resolved_args.append(('literal', arg))
        elif isinstance(arg, str) and arg in DATA_REF_MAP:
            resolved_args.append(('data', arg))
        else:
            resolved_args.append(('literal', arg))

    return ctx.add_indicator(func, tuple(resolved_args), field)


def _resolve_data_ref(defn: dict, ctx: _CompileContext) -> str:
    """解析数据引用"""
    data_name = defn.get('data', 'close')
    shift = defn.get('shift', 0)

    if data_name not in DATA_REF_MAP:
        logger.warning(f'未知数据引用: {data_name}，fallback 到 close')
        data_name = 'close'

    ref_key = f'data_{data_name}'
    if shift != 0:
        ref_key = f'{ref_key}_shift_{shift}'

    if ref_key not in ctx.data_refs:
        ctx.data_refs[ref_key] = {
            'data': data_name,
            'shift': shift,
        }
    return ref_key


# ============================================================
# Step 3: 动态生成策略类
# ============================================================


def _build_strategy_class(
    ctx: _CompileContext,
    bt_params: tuple,
    class_name: str,
    strategy_name: str,
    buy_tree: RuleNode,
    sell_tree: RuleNode,
    params_raw: Dict[str, Any],
    chart_indicators: list,
    meta_description: str,
) -> Type[StockSolveStrategy]:
    """用 type() 动态生成策略类"""

    # ---- 闭包引用，避免 self 冲突 ----
    _ctx = ctx
    _buy_tree = buy_tree
    _sell_tree = sell_tree
    _params_raw = params_raw
    _chart_indicators = chart_indicators
    _strategy_name = strategy_name
    _meta_description = meta_description
    _all_params = bt_params

    # ---- __init__ ----
    def __init__(self):
        StockSolveStrategy.__init__(self)

        # 1. 实例化指标
        for ind_key, info in _ctx.indicators.items():
            indicator = _create_bt_indicator(self, info)
            setattr(self, f'_{ind_key}', indicator)

        # 2. 实例化 CrossOver
        for left_key, right_key in _ctx.cross_entries:
            left_line = getattr(self, f'_{left_key}')
            right_line = getattr(self, f'_{right_key}')
            cross = bt.indicators.CrossOver(left_line, right_line)
            setattr(self, f'_cross_{left_key}_{right_key}', cross)

        # 3. 存储字面量
        self._literals = dict(_ctx.literals)

    # ---- on_bar ----
    def on_bar(self):
        # scan 模式：无状态逐 bar 评估买卖条件，只记录命中、不下单
        # （不依赖 self.position，因此可在无持仓时评估 sell_tree）
        if self.p._scan_mode:
            buy_hit = _eval_rules(_buy_tree, self)
            sell_hit = _eval_rules(_sell_tree, self)
            if buy_hit or sell_hit:
                self._signal_log.append({
                    'date': self.data.datetime.date(0).strftime('%Y-%m-%d'),
                    'buy': bool(buy_hit),
                    'sell': bool(sell_hit),
                })
            return
        # 默认模式：原交易逻辑
        if not self.position:
            if _eval_rules(_buy_tree, self):
                self.buy_with_reason(_make_reason(_buy_tree, self, '买入'))
        else:
            if _eval_rules(_sell_tree, self):
                self.sell_with_reason(_make_reason(_sell_tree, self, '卖出'))

    # ---- get_params_info ----
    @classmethod
    def get_params_info(cls):
        schema = []
        for key, info in _params_raw.items():
            if isinstance(info, dict):
                schema.append({
                    'key': key,
                    'label': info.get('label', key),
                    'type': info.get('type', 'float'),
                    'default': info.get('value', info.get('default', 0)),
                    'min': info.get('min', 0),
                    'max': info.get('max', 999999),
                    'unit': info.get('unit', ''),
                })
            else:
                schema.append({
                    'key': key,
                    'label': key,
                    'type': 'float',
                    'default': info,
                    'min': 0,
                    'max': 999999,
                    'unit': '',
                })
        return schema

    # ---- get_chart_indicators ----
    @classmethod
    def get_chart_indicators(cls, params: dict):
        """解析 chart_indicators 模板中的 {param} 引用

        优先从传入 params 取值，其次从类默认 params 取值，
        都无法解析时保留原始字符串（由下游按需处理）。
        """
        # 构建默认值字典
        defaults = {}
        for key, info in _params_raw.items():
            if isinstance(info, dict):
                defaults[key] = info.get('value', info.get('default', ''))
            else:
                defaults[key] = info

        result = []
        for item in _chart_indicators:
            resolved = {}
            for k, v in item.items():
                if isinstance(v, str) and v.startswith('{') and v.endswith('}'):
                    param_name = v[1:-1]
                    # 优先传入值，其次默认值，最后保留原始模板
                    if param_name in params:
                        resolved[k] = params[param_name]
                    elif param_name in defaults:
                        resolved[k] = defaults[param_name]
                    else:
                        resolved[k] = v
                else:
                    resolved[k] = v
            result.append(resolved)
        return result

    # ---- 组装类 ----
    cls_dict = {
        'params': _all_params,
        '__init__': __init__,
        'on_bar': on_bar,
        'get_params_info': get_params_info,
        'get_chart_indicators': get_chart_indicators,
        '_strategy_name': _strategy_name,
        '_meta_description': _meta_description,
    }

    return type(class_name, (StockSolveStrategy,), cls_dict)


# ============================================================
# 指标实例化
# ============================================================


def _create_bt_indicator(strategy_instance, info: dict):
    """根据指标信息创建 Backtrader 指标实例"""
    func = info['func']
    field = info.get('field')
    args = info['args']

    # 解析 args
    resolved_args = []
    for arg in args:
        arg_type = arg[0]
        arg_value = arg[1]
        if arg_type == 'param':
            resolved_args.append(getattr(strategy_instance.p, arg_value))
        elif arg_type == 'data':
            resolved_args.append(getattr(strategy_instance.data, arg_value))
        elif arg_type == 'literal':
            resolved_args.append(arg_value)

    # volume 特殊处理：直接返回 self.data.volume
    if func == 'volume':
        return strategy_instance.data.volume

    # 查找指标映射
    indicator_info = INDICATOR_MAP.get(func)
    if not indicator_info:
        raise ValueError(f'未知指标函数: {func}，支持: {list(INDICATOR_MAP.keys())}')

    bt_cls = indicator_info['cls']
    arg_names = indicator_info['arg_names']
    fixed_kwargs = indicator_info.get('fixed_kwargs', {})

    # 特殊处理 KDJ: args 顺序是 (high, low, close, n)
    # Backtrader Stochastic 从绑定的 data feed 自动取 high/low/close，
    # 因此不传 data line，让其自动绑定到策略 data feed
    if func == 'kdj':
        period = resolved_args[3] if len(resolved_args) > 3 else 9
        indicator = bt_cls(
            period=period,
            period_dfast=fixed_kwargs.get('period_dfast', 3),
        )
    # CYW: 同花顺主力控盘需要 high/low/close/volume，不传 data line，
    # 由 Backtrader 自动绑定到策略 data feed
    elif func == 'cyw':
        period = resolved_args[1] if len(resolved_args) > 1 else 10
        indicator = bt_cls(period=period)
    # MACD: args 顺序 (close, fast, slow, signal) → BT 用 period_me1, period_me2, period_signal
    elif func == 'macd':
        kwargs = {
            'period_me1': resolved_args[1] if len(resolved_args) > 1 else 12,
            'period_me2': resolved_args[2] if len(resolved_args) > 2 else 26,
            'period_signal': resolved_args[3] if len(resolved_args) > 3 else 9,
        }
        indicator = bt_cls(resolved_args[0], **kwargs)
    else:
        # 通用构造: resolved_args[0] 是 data line（位置参数），其余是关键字参数
        kwargs = dict(fixed_kwargs)
        for i, name in enumerate(arg_names):
            if i == 0:
                continue  # data line 走位置参数
            if i < len(resolved_args):
                kwargs[name] = resolved_args[i]
        indicator = bt_cls(resolved_args[0], **kwargs)

    # 多线指标：提取字段
    if field and field in FIELD_MAP:
        attr = FIELD_MAP[field]
        if attr is None:
            # J 线：3*K - 2*D
            return indicator.percK * 3 - indicator.percD * 2
        return getattr(indicator, attr)

    return indicator


# ============================================================
# 规则评估
# ============================================================


def _eval_rules(node: RuleNode, strategy_instance) -> bool:
    """递归评估规则树"""
    if node.is_leaf:
        if node.op == 'empty':
            return False
        return _eval_leaf(node, strategy_instance)
    elif node.logic == 'AND':
        return all(_eval_rules(r, strategy_instance) for r in node.rules)
    elif node.logic == 'OR':
        return any(_eval_rules(r, strategy_instance) for r in node.rules)
    return False


def _eval_leaf(node: RuleNode, strategy_instance) -> bool:
    """评估叶子节点"""
    op = node.op

    if node.is_cross:
        # cross_above / cross_below：从 CrossOver 指标取值
        cross = getattr(strategy_instance, f'_cross_{node.left_ref}_{node.right_ref}')
        if op == 'cross_above':
            return cross[0] > 0
        elif op == 'cross_below':
            return cross[0] < 0
        return False

    # 普通比较
    left_val = _resolve_ref(node.left_ref, strategy_instance)
    right_val = _resolve_ref(node.right_ref, strategy_instance)

    if op == '>':
        return left_val > right_val
    elif op == '<':
        return left_val < right_val
    elif op == '>=':
        return left_val >= right_val
    elif op == '<=':
        return left_val <= right_val
    elif op == '==':
        return left_val == right_val
    elif op == '!=':
        return left_val != right_val

    return False


def _resolve_ref(ref_key: str, strategy_instance):
    """根据引用 key 获取当前 bar 的值"""
    if ref_key is None:
        return 0

    # 字面量
    if ref_key.startswith('val_'):
        return strategy_instance._literals.get(ref_key, 0)

    # 参数引用
    if ref_key.startswith('param:'):
        param_name = ref_key[6:]
        return getattr(strategy_instance.p, param_name)

    # 数据引用
    if ref_key.startswith('data_'):
        # 解析 data_name[_shift_N]
        parts = ref_key[5:].split('_shift_')
        data_name = parts[0]
        shift = int(parts[1]) if len(parts) > 1 else 0
        line = getattr(strategy_instance.data, data_name, strategy_instance.data.close)
        return line[shift]

    # 指标引用
    indicator = getattr(strategy_instance, f'_{ref_key}', None)
    if indicator is not None:
        # 处理 Backtrader line 对象
        if hasattr(indicator, '__getitem__'):
            return indicator[0]
        # CrossOver 也走这里
        return indicator[0] if hasattr(indicator, '__getitem__') else indicator

    return 0


def _make_reason(node: RuleNode, strategy_instance, prefix: str) -> str:
    """生成信号原因描述"""
    if node.is_leaf:
        if node.is_cross:
            return f'{prefix}信号: {node.left_ref} {node.op} {node.right_ref}'
        return f'{prefix}信号'
    return f'{prefix}信号 ({node.logic})'


# ============================================================
# 辅助函数
# ============================================================


def _to_class_name(name: str) -> str:
    """将策略名转为合法的 Python 类名"""
    # 去除特殊字符，首字母大写
    clean = re.sub(r'[^a-zA-Z0-9_一-鿿]', '_', name)
    if clean and clean[0].isdigit():
        clean = '_' + clean
    # 如果全是中文，加前缀
    if re.match(r'^[一-鿿]', clean):
        return f'JsonStrategy_{clean[:20]}'
    return clean[:50] or 'JsonStrategy'
