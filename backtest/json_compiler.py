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
import math
import re
import logging
from typing import Dict, Any, List, Optional, Tuple, Type

import backtrader as bt

from backtest.strategy_base import StockSolveStrategy
from backtest.dpo_indicator import DPO
from backtest.dma_indicator import DMA
from backtest.cyw_indicator import CYW

logger = logging.getLogger(__name__)

# 回看越过数据起点时的哨兵：调用方按「条件此刻不成立」处理。
# 若改成抛 IndexError，异常会冒进 backtrader 把整轮回测打成 error，
# 把本可解释的「这根 bar 条件不成立」变成失败。
_UNAVAILABLE = object()

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
        # v3 S4-B：策略用到的因子字段（决定数据源要带哪些 f_<field> 线）
        self.factor_fields: set = set()
        # 计数器
        self._ind_counter = 0
        self._val_counter = 0
        self.unsupported: List[str] = []
        # 「引用了不存在的参数」这类操作数：运行时应判假而不是拿 0 去比较
        # （拿 0 会让 close > 0 这种条件恒真 —— 悄悄变成另一个策略，比崩更危险）
        self.bad_refs: set = set()
        # 能力缺口（结构化）：模型**想要**但引擎表达不了的东西。
        # 与 unsupported 的分工：unsupported 记「引擎丢了什么」，gaps 记「模型想要什么」——
        # 后者才是「引擎该扩哪块」的依据（实测 2026-10-14：模型想要「20 日新高」和「1.5 倍」，
        # 引擎当时都没有，只能改写成布林上轨，而系统一句提示都没有）。
        self.gaps: List[Dict[str, Any]] = []
        # 外部收集器：编译中途抛错（未知指标会 raise）时，缺口也不会丢
        self.gap_sink: Optional[List[Dict[str, Any]]] = None

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

    def add_gap(self, kind: str, name: str, detail: str = '') -> None:
        """记一条能力缺口（去重、保序）：kind ∈ indicator / data / operator / temporal"""
        item = {'kind': str(kind), 'name': str(name), 'detail': str(detail)[:200]}
        if item not in self.gaps:
            self.gaps.append(item)
        if self.gap_sink is not None and item not in self.gap_sink:
            self.gap_sink.append(item)

    def add_unsupported(self, message: str) -> None:
        """记录一条引擎表达不了、会被静默丢弃的条件（去重、保序）"""
        if message not in self.unsupported:
            self.unsupported.append(message)


# ============================================================
# 指标函数映射
# ============================================================

# func_name → (Backtrader 指标类, 构造函数参数有序列表)
# args 顺序对应 JSON args，构造时按关键字传入
class RollingExtreme(bt.Indicator):
    """N 根内的最高 / 最低值（自己实现，替代 bt.indicators.Highest/Lowest）。

    「N 日新高」是用户最常说的条件之一（2026-10-14 那次质询就是它）。
    自己实现的好处：语义在我们手里（窗口口径、minperiod 都是显式的），
    不依赖内置类在本机未经验证的运行时行为（cerebro 真跑验证目前受阻，见文件内注释）。
    """

    lines = ('out',)
    params = (('period', 20), ('mode', 'max'))

    def __init__(self):
        super().__init__()
        self.addminperiod(int(self.p.period))

    def next(self):
        n = max(1, int(self.p.period))
        window = self.data.get(size=n)
        self.lines.out[0] = max(window) if str(self.p.mode) == 'max' else min(window)


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
    # ⚠️ safediv=True 是**必需**项，不是可选优化：RSI 内部 rs = maup / madown，
    # 窗口内没有任何下跌（madown=0）时除零会抛 ZeroDivisionError，把**整轮**回测打挂。
    # 真机事故（2026-10-14）：研发第 4 轮生成 RSI 周期 2（极短窗口必然出现无下跌段），
    # 报「回测执行失败：float division by zero」，整个 8 轮研发任务被判 failed。
    # safediv=True 走 bt.DivZeroByZero：x/0 取 safehigh=100（全涨=RSI 100，语义正确），
    # 0/0 取 safelow=50（平价窗口取中性值）。
    'rsi': {
        'cls': bt.indicators.RSI,
        'arg_names': ['data', 'period'],
        'fixed_kwargs': {'safediv': True},
    },
    'macd': {
        'cls': bt.indicators.MACD,
        'arg_names': ['data', 'period_me1', 'period_me2', 'period_signal'],
    },
    # ⚠️ safediv 同理：K = 100 * (close - lowest) / (highest - lowest)，
    # 窗口内 highest == lowest（一字板 / 平价）时除零，同样打挂整轮。
    # safediv=True 走 bt.DivByZero(zero=0.0)，与同花顺 IF(HIGH>LOW,...) 口径一致。
    'kdj': {
        'cls': bt.indicators.Stochastic,
        'arg_names': ['data', 'period', 'period_dfast'],
        'fixed_kwargs': {'period_dfast': 3, 'safediv': True},
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
    # N 日新高 / 新低：没有它，LLM 只能用布林上轨或「高点均线」凑「创 20 日新高」，
    # 语义就变了（实测 2026-10-14：用户说「收盘价创 20 日新高」，生成的是
    # close > boll().upper，量能 1.5 倍门槛也被悄悄去掉）。
    # ⚠️ 用自实现滚动极值（RollingExtreme）而不是 bt.indicators.Highest/Lowest：
    # 本机对「真跑 cerebro」的验证目前受阻（测试文件里最简单的 MA5 策略也会挂住，
    # 已排除 volume 列与 Highest 两个嫌疑），在根因定位前不依赖未经验证的内置类。
    'highest': {
        'cls': RollingExtreme,
        'arg_names': ['data', 'period'],
        'fixed_kwargs': {'mode': 'max'},
    },
    'lowest': {
        'cls': RollingExtreme,
        'arg_names': ['data', 'period'],
        'fixed_kwargs': {'mode': 'min'},
    },
}

# 数据名称 → self.data.xxx 的属性路径
# ⚠️ 这里列出的每个字段都必须在 ``AStockPandasData`` 里真实接线（含 pre_close /
# pct_change / amount / change_amount）。2026-10-20 排查：turnover_rate / amplitude
# 声明了但库里 100% 为空、feed 也没接，运行时被 getattr(..., close) 静默替换成
# 收盘价——「涨幅 >5%」变成「价格 >5 元」。在数据补齐之前不再对外宣称这两个字段。
DATA_REF_MAP: Dict[str, str] = {
    'close': 'close',
    'open': 'open',
    'high': 'high',
    'low': 'low',
    'volume': 'volume',
    'amount': 'amount',
    'pre_close': 'pre_close',
    'pct_change': 'pct_change',
    'change_amount': 'change_amount',
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

# 指标人话解释（工作台 v3 · F3 参数可读化）：编辑器下拉 hover 与参数区展示。
# 原则：说清「这是什么、调大调小会怎样」，**不构成买卖建议**（触轨≠反转）。
INDICATOR_HELP: Dict[str, str] = {
    'ma': '简单移动平均。周期越大越平滑，信号越少、滞后越多。',
    'ema': '指数移动平均，对近期价格更敏感，比 MA 反应快。',
    'wma': '加权移动平均，权重线性递减，特性介于 MA 与 EMA 之间。',
    'rsi': '相对强弱指数（0-100）。低于 30 常视为超卖、高于 70 超买——是状态不是买卖指令。',
    'macd': '快慢均线之差。金叉/死叉看趋势转向，柱状图看动能强弱。',
    'kdj': '随机指标，看 K/D 交叉与 J 值极值。短周期灵敏、区间行情易钝化。',
    'boll': '布林带：中轨是均线，上下轨是 ±N 倍标准差。触轨不代表会反转。',
    'atr': '真实波幅均值，衡量波动大小。常用来定止损距离，不是方向信号。',
    'volume': '成交量。放量/缩量要配合价格条件用，单独无方向含义。',
    'dpo': '区间震荡线：去掉长周期趋势后看短期摆动的周期性。',
    'dma': '两条均线的差值再平滑，类似 MACD 但更直接。',
    'cyw': '主力控盘线，衡量主力持仓意愿（算法口径以引擎实现为准）。',
    'highest': 'N 根内的最高价。常用于「N 日新高」类条件。',
    'lowest': 'N 根内的最低价。常用于「N 日新低」类条件。',
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
    'pct_change': '涨跌幅（百分数，5 = 5%）',
    'change_amount': '涨跌额',
}


def get_strategy_meta() -> dict:
    """导出策略元数据，供前端渲染可视化构造器"""
    indicators = {}
    for func_name, info in INDICATOR_MAP.items():
        indicators[func_name] = {
            'label': INDICATOR_LABELS.get(func_name, func_name),
            'args': info['arg_names'],
            'fields': _fields_for_func(func_name),
            'help': INDICATOR_HELP.get(func_name, ''),
        }

    operators = [{'value': k, 'label': v} for k, v in OPERATOR_LABELS.items()]

    data_refs = [{'value': k, 'label': v} for k, v in DATA_REF_LABELS.items()]

    # v3 S4-B：因子操作数（时序语义：该票自己的因子值当指标用，不是横截面排名）
    try:
        from backtest.factor_lib import factor_meta
        # factor_meta 的键是 factor_id/name，这里转成前端用的 field/label
        factors = [{'field': f['factor_id'], 'label': f['name'],
                    'category': f.get('category', ''),
                    'description': f.get('description', ''),
                    'min_rows': f.get('min_rows', 20)} for f in factor_meta()]
    except Exception:  # noqa: BLE001
        factors = []

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
        # v3 S4-B：因子操作数。前端构造器渲染「因子」下拉用。
        # ⚠️ 语义提醒必须留在 UI 上：这是**该票自己的因子值**（时序），
        # 与因子评估页的 RankIC（横截面排名）不是一回事。
        'factors': factors,
        'templates': templates,
        'conditions': {'buy': '买入条件', 'sell': '卖出条件'},
        # hold_bars：持有期卖点（前端构造器需要它渲染「持有 N 根后卖出」）
        'exit_modes': {
            'hold_bars': {'label': '持有 N 根 K 线后卖出', 'type': 'int', 'min': 1, 'default': 0,
                          'help': '纯时序卖点。0 = 不启用；如「尾盘买入、次日卖出」填 1，'
                                  '此时不要再编造卖出指标条件。'},
        },
    }


def export_capabilities() -> Dict[str, Any]:
    """引擎能力表（结构化，**单一事实源**）。

    用途三处共用，避免各写一份互相漂移：
    1. 提示词（export_llm_schema 引用它）；
    2. 前端「能力清单」页；
    3. 能力缺口判定（模型要的东西在不在这里）。
    """
    return {
        'data_fields': sorted(DATA_REF_MAP.keys()),
        'indicators': {name: {'args': list(info.get('arg_names') or [])}
                       for name, info in INDICATOR_MAP.items()},
        'operators': sorted(COMPARE_OPS | CROSS_OPS),
        'temporal': {
            'shift': '数据引用支持 shift=N（N 根之前；不支持负数）',
            'hold_bars': 'conditions.sell.hold_bars = 持有 N 根后卖出（纯时序卖点）',
        },
        'timings': ['next_open', 'same_close', 'next_close'],
        'special_forms': {
            'highest': 'N 日新高：{"func":"highest","args":["high",N]}',
            'lowest': 'N 日新低：{"func":"lowest","args":["low",N]}',
            'mul': '倍数：{"func":"mul","args":[<操作数>,倍数]}（倍数是 JSON 数字 1.5，不能写 "1.5"）',
        },
        # 实验性能力：2026-10-14 起 highest / lowest / mul 已通过真跑验证（编译 + 取值 + 预热根数），
        # 不再是实验项。此前「真跑会卡住」的真根因是指标参数字典形态被当字面量塞进 bt（见
        # _resolve_indicator 注释），与这三项能力无关。此列表留作后续新能力的暂存位。
        'experimental': [],
    }


def export_llm_schema() -> str:
    """只读导出编译器的指标/字段/数据/运算符清单，作为 LLM prompt 约束（A3 schema 单源）。

    与 get_strategy_meta 同模式的只读导出，不改编译逻辑。
    供 StrategyGenerator 与 /strategies/generate 端点共用（M4 单源化）。
    """
    lines: List[str] = []
    lines.append('## 可用指标函数（func 只能取以下值）')
    for func, info in INDICATOR_MAP.items():
        fields = _fields_for_func(func)
        field_names = [f['value'] for f in fields]
        if func == 'volume':
            arg_str = '（无参数，直接代表成交量线）'
            field_str = '（无 field）'
        else:
            args = info['arg_names']
            arg_str = '(' + ', '.join(args) + ')'
            field_str = '可选 field: ' + (', '.join(field_names) if field_names else '（无，单线指标不带 field）')
        lines.append(f'- {func}{arg_str}；{field_str}')
    lines.append('')
    lines.append('## 字段（field）全局取值')
    lines.append(', '.join(sorted(FIELD_MAP.keys())))
    lines.append('')
    lines.append('## 数据引用（"data" 只能取以下值）')
    lines.append(', '.join(sorted(DATA_REF_MAP.keys())))
    lines.append('⚠️ pct_change 的单位是**百分数**（5 表示 5%），不是 0.05；'
                 'amount 的单位是元；change_amount 的单位是元。')
    lines.append('')
    lines.append('## 运算符（op 只能取以下值）')
    lines.append('比较: ' + ', '.join(sorted(COMPARE_OPS)))
    lines.append('跨越: ' + ', '.join(sorted(CROSS_OPS)) + '（cross_above/cross_below 左右必须是指标定义 {"func": ...}）')
    lines.append('')
    lines.append('## 结构硬性要求')
    lines.append('1. 顶层键: meta / params / conditions / risk / chart_indicators（后两者可选）；')
    lines.append('2. params 中每个参数必须是 {"value": 数值, "min": 数值, "max": 数值, "label": 文本, "type": "int"|"float"}；')
    lines.append('3. conditions.buy 必须存在且 rules 非空；')
    lines.append('   conditions.sell 的 rules 可为空 —— 纯时序卖点（持有 N 根后无条件卖出）用hold_bars 表达，不要用指标条件凑；')
    lines.append('4. 持有期卖点: conditions.sell.hold_bars = 整数 N（>=1），表示买入后持有 N 根 K 线无条件卖出。');
    lines.append('   「尾盘买入、次日卖出」= conditions.sell.hold_bars = 1（此时不要写任何 sell.rules）；')
    lines.append('   「尾盘买入、持股3 天」= hold_bars = 3。hold_bars 与 sell.rules 可并存，hold_bars 优先触发；')
    lines.append('5. 规则叶子结构: {"type": "indicator", "left": {...}, "op": "...", "right": {...}}；')
    lines.append('   左/右值只能是三种之一: {"func": "指标名", "args": [...], "field": 可选} / {"value": 数值} / {"data": "数据名", "shift": 可选}；')
    lines.append('   其中 "shift": N 表示「N 根之前」的取值（1 = 昨收/昨日；不支持负数，负数属于前视偏差）；')
    lines.append('   「N 日新高」用 {"func": "highest", "args": ["close", N]}（收盘价创 N 日新高），')
    lines.append('   不要写成 ["high", N] —— high 恒 >= close，条件几乎不可能成立；「N 日新低」同理用 ["close", N]；')
    lines.append('   「1.5 倍」这类倍数用 {"func": "mul", "args": [<操作数>, 倍数]} 精确表达，不要丢掉倍数；')
    lines.append('   ⚠️ 倍数必须是 JSON **数字**（1.5 / 2 / 1.08），**不能写成字符串**（"1.5"）或带单位的 "1.5倍"，')
    lines.append('      否则编译期按「倍数不是数值」判该条件恒假（真机多次踩到，重试才修好）；')
    lines.append('   例：成交量大于 20 日均量的 1.5 倍 = {"func": "mul", "args": [{"func": "ma", "args": ["volume", 20]}, 1.5]}。');
    lines.append('6. 指标 args 里：数据引用写成字符串 "close"/"high"，参数引用写 "{参数名}"，字面量直接写数值；')
    lines.append('   （也接受 {"data": "close"} / {"value": N} / {"param": "fast"} 这种对象写法；'
                 '不要写 ["close"] 这类数组，编译期会拒收）')
    lines.append('7. KDJ 固定写法: {"func": "kdj", "args": ["high", "low", "close", "{kdj_n}"], "field": "k"|"d"|"j"}；')
    lines.append('8. 严禁使用清单之外的指标函数（如 obv/vwap/adx/cci/wr 等）和清单之外的数据引用；')
    lines.append('9. 指标的 field 必须属于该指标的合法 field 列表（见上方每个指标的可选 field）；')
    lines.append('10. 成交口径（买入/卖出时点）由系统参数决定，不写进 JSON。不要为了表达「尾盘买入」去编造指标条件；')
    lines.append('11. 只输出一个合法 JSON 对象，不要任何解释文字、不要 markdown 代码块围栏。')
    return '\n'.join(lines)


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


def compile_strategy(json_str: str, gap_sink: Optional[List[Dict[str, Any]]] = None) -> Type[StockSolveStrategy]:
    """将 JSON 策略定义编译为 Backtrader 策略类

    Args:
        json_str: 符合 strategy-schema 的 JSON 字符串

    Returns:
        StockSolveStrategy 的子类，可直接传给 cerebro.addstrategy()
    """
    data = json.loads(json_str)

    # 编译期 field 校验（A3 Spike 修订 1）：
    # FIELD_MAP 查找原本发生在运行时 _create_bt_indicator，编译通过 ≠ 回测可跑。
    # 此处在编译期即拒绝「field 不属于该指标」的策略，错误信息自带合法 field 清单。
    _validate_fields(data)

    ctx = _CompileContext()
    ctx.gap_sink = gap_sink          # 编译失败（raise）时缺口照样落到调用方的清单里

    # Step 1: 解析 params
    params_raw = data.get('params', {})
    bt_params = _parse_params(params_raw, ctx)

    # Step 2: 解析 conditions → 收集指标 + 构建规则树
    conditions = data.get('conditions', {})
    buy_rules = conditions.get('buy', {}).get('rules', [])
    sell_rules = conditions.get('sell', {}).get('rules', [])
    buy_logic = conditions.get('buy', {}).get('logic', 'AND')
    sell_logic = conditions.get('sell', {}).get('logic', 'AND')

    # hold_bars：「买入后持有 N 根 K 线无条件卖出」。
    # 这是「尾盘买入、次日卖出」这类**纯时序**策略唯一正确的表达方式 —— 此前 DSL
    # 只有指标比较叶子，LLM 表达不了时序卖点，只能自己编 MA20/RSI/MACD 之类无关条件
    # 凑数，结果与用户原意完全不符。
    hold_bars = _parse_hold_bars(conditions, ctx)

    buy_tree = _build_rule_tree(buy_rules, buy_logic, ctx)
    sell_tree = _build_rule_tree(sell_rules, sell_logic, ctx)

    # 买入条件缺失是硬错误（没有任何买入触发 = 永不交易，静默跑出 0 笔更骗人）；
    # 卖出条件缺失则允许 —— 有 hold_bars 就能表达纯时序卖出。
    if not buy_rules:
        ctx.add_unsupported('conditions.buy.rules 为空：策略没有任何买入触发点，不会产生交易')
    if not sell_rules and not hold_bars:
        ctx.add_unsupported('conditions.sell.rules 为空且未给 hold_bars：策略没有任何卖出触发点')

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
        hold_bars=hold_bars,
        params_raw=params_raw,
        chart_indicators=chart_indicators,
        meta_description=meta.get('description', ''),
    )

    strategy_cls._unsupported_conditions = list(ctx.unsupported)
    strategy_cls._bad_refs = set(ctx.bad_refs)
    strategy_cls._capability_gaps = list(ctx.gaps)
    # v3 S4-B：本策略用到的因子字段；引擎据此给数据源补 f_<field> 线
    strategy_cls._factor_fields = frozenset(ctx.factor_fields)

    return strategy_cls


def _parse_hold_bars(conditions: Dict[str, Any], ctx: _CompileContext) -> int:
    """解析 hold_bars（买入后持有 N 根 K 线无条件卖出）。

    写在 ``conditions.sell.hold_bars``（推荐）或顶层 ``conditions.hold_bars``，
    兼容 LLM 可能给出的两种位置。同时接受 ``{"value": 1}`` 包装与裸数值。
    """
    raw = None
    sell_cfg = conditions.get('sell')
    if isinstance(sell_cfg, dict):
        raw = sell_cfg.get('hold_bars')
    if raw is None:
        raw = conditions.get('hold_bars')

    if raw is None:
        return 0
    if isinstance(raw, dict):
        raw = raw.get('value')
    try:
        n = int(raw)
    except (TypeError, ValueError):
        ctx.add_unsupported(f'hold_bars 取值非法（{raw!r}），已按不启用处理')
        return 0
    if n < 1:
        if n != 0:
            ctx.add_unsupported(f'hold_bars 必须 >= 1（收到 {n}），已按不启用处理')
        return 0
    return n


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


def _normalize_operand(defn: Any) -> Optional[Dict[str, Any]]:
    """操作数形态归一化：合法对象原样返回，裸标量包成对象，无法识别返回 None。

    规范写法是 {"value": N} / {"data": "close"} / {"func": ...}，但 LLM 与手写 JSON
    经常直接写 "right": 50 / "left": "close"。旧实现只认 dict，于是把合法条件当成
    「缺 left/right」处理并**把右值改成 1**（rsi > 50 静默变成 rsi > 1）——条件看起来
    生效了、其实换了个阈值。这里统一归一化，让引擎真正按写下的条件跑；
    实在无法识别的形态才返回 None（调用方按恒假处理）。
    """
    if isinstance(defn, dict):
        return defn or None
    if isinstance(defn, bool):
        return None
    if isinstance(defn, (int, float)):
        return {"value": defn}
    if isinstance(defn, str):
        text = defn.strip()
        if not text:
            return None
        if text.startswith("{") and text.endswith("}"):
            return {"param": text[1:-1].strip()}
        if text in DATA_REF_MAP:
            return {"data": text}
        return None
    return None


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
            left_def = _normalize_operand(rule.get('left'))
            right_def = _normalize_operand(rule.get('right'))
            if left_def is None or right_def is None:
                # 缺操作数：这一片叶子必须**恒假**。
                # ⚠️ 不能塞 0/1 顶替：{"value":0} < {"value":1} 在 '<' 下恒**真**，
                # 畸形规则会变成「每根都买」——比报错危险得多。
                msg = f'条件缺少 left/right，已按恒假处理: {rule}'
                logger.warning(msg)
                ctx.add_unsupported(msg)
                leaf = RuleNode()
                leaf.op = 'empty'          # _eval_rules 见到 empty 直接返回 False
                leaf.left_ref = 'val_empty'
                leaf.right_ref = 'val_empty'
                leaf.is_cross = False
                node.rules.append(leaf)
                continue
            if op not in COMPARE_OPS and op not in CROSS_OPS:
                msg = f'不支持的运算符 op={op}，已按恒假处理'
                logger.warning(msg)
                ctx.add_unsupported(msg)

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
            msg = f'未知规则类型: {rule_type}，已跳过该条件'
            logger.warning(msg)
            ctx.add_unsupported(msg)

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
        # ⚠️ LLM 常把「参数引用」又包一层 value：{"value": {"param": "rsi_floor"}}。
        # 早期实现把内层 dict 直接当字面量存下来，运行到比较时才炸：
        #   TypeError: '>' not supported between instances of 'float' and 'dict'
        # 而异常冒到引擎层会把**整轮回测**记成「回测执行失败」（实测 SR-20261012-1EAEF9
        # 第 8 轮就是这样丢的）。这里按内层形态解包，并把这次纠偏如实记进 unsupported，
        # 让「引擎替模型纠了偏」在体检/账本里看得见，而不是静默通过。
        if isinstance(raw, dict):
            inner = {k: raw[k] for k in ('value', 'param', 'data', 'func', 'pattern') if k in raw}
            if inner:
                ctx.add_unsupported(
                    '操作数写成 {"value": {...}} 的嵌套形态，已按内层定义解析: ' + str(raw)[:80])
                return _resolve_value(inner, ctx)
        # {param} 引用走运行时参数解析
        if isinstance(raw, str) and raw.startswith('{') and raw.endswith('}'):
            param_name = raw[1:-1]
            if param_name in ctx.param_names:
                return f"param:{param_name}"
        return ctx.add_literal(raw)
    if str(defn.get('func') or '') == 'mul':
        # 倍数（如「成交量 > 20 日均量的 1.5 倍」）：{"func":"mul","args":[<操作数>, 1.5]}
        # 没有它，LLM 只能把「1.5 倍」这个约束丢掉（实测确实丢了），条件语义随之改变。
        args = defn.get('args') or []
        if len(args) >= 2:
            inner = args[0] if isinstance(args[0], dict) else {'data': str(args[0])}
            factor_arg = args[1]
            if isinstance(factor_arg, dict) and 'value' in factor_arg:
                factor_arg = factor_arg.get('value')
            try:
                factor = float(factor_arg)
            except (TypeError, ValueError):
                # ⚠️ 不能退化成 0：「成交量 > 20 日均量 × 0」会变成恒真，
                # 条件直接失效还看不出来（比恒假危险得多）⇒ 按恒假处理并记账。
                ctx.add_unsupported(f'mul 的倍数 {args[1]!r} 不是数值，该条件按恒假处理')
                key = ctx.add_literal(0)
                ctx.bad_refs.add(key)
                return key
            return 'mul:' + _resolve_value(inner, ctx) + ':' + repr(factor)
        ctx.add_unsupported('mul 需要两个参数（操作数、倍数），已按恒假处理')
        key = ctx.add_literal(0)
        ctx.bad_refs.add(key)
        return key
    if 'func' in defn:
        return _resolve_indicator(defn, ctx)
    if 'factor' in defn:
        # v3 S4-B：因子作为操作数（时序语义）。不是 Backtrader Indicator 包装
        # （那样每根 bar 重算一次全序列 = O(n²)），而是**数据线**：编译期登记
        # factor_fields，由数据源一次性算好 f_<field> 线，条件直接取当前 bar 的线值。
        field = str(defn.get('factor') or '').strip()
        from backtest.factor_lib import FACTOR_IDS
        if not field or field not in FACTOR_IDS:
            ctx.add_unsupported(f"因子 {field!r} 不在因子注册表里，该条件按恒假处理")
            key = ctx.add_literal(0)
            ctx.bad_refs.add(key)
            return key
        ctx.factor_fields.add(field)
        return f'f_{field}'
    if 'data' in defn:
        data_name = str(defn.get('data') or '')
        if data_name and data_name not in DATA_REF_MAP:
            ctx.add_gap('data', data_name, '数据字段不在引擎能力表里')
        return _resolve_data_ref(defn, ctx)
    if 'pattern' in defn:
        # 蜡烛形态暂不支持，返回字面量 False
        logger.warning(f"蜡烛形态暂不支持: {defn['pattern']}")
        ctx.add_unsupported(f'pattern 条件暂不支持，已按恒假处理: {defn}')
        return ctx.add_literal(False)
    if 'param' in defn:
        # 参数引用在运行时替换；⚠️ 未声明的参数名会让 getattr(p, name) 抛 AttributeError
        # 并拖垮整轮回测 —— 编译期就拦下来，按恒假处理并记账（不静默、不崩）
        name = str(defn['param'])
        if name and name in ctx.param_names:
            return f"param:{name}"
        ctx.add_unsupported(
            f'参数引用 {name!r} 未在 params 里声明，该条件按恒假处理（否则运行时会崩）')
        key = ctx.add_literal(0)
        ctx.bad_refs.add(key)
        return key
    # 默认字面量
    msg = f'无法识别的操作数形态，已按字面量 0 处理（该条件恒假）: {defn}'
    logger.warning(msg)
    ctx.add_unsupported(msg)
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
            ctx.add_gap('indicator', str(func), '指标函数不在引擎能力表里')
            raise ValueError(
                f"未知指标函数: '{func}'。\n"
                f"支持的指标函数: {list(INDICATOR_MAP.keys())}"
            )

    # 解析 args：兼容四种写法（LLM 会混用，全部在编译期归一化）
    #   数值                        → 字面量
    #   "close"                     → 数据引用（DATA_REF_MAP 里的名字）
    #   "{fast}"                    → 参数引用（已声明的策略参数）
    #   {"data": "close"} / {"value": 5} / {"param": "fast"} → 与规则左右值同一套写法
    # ⚠️ 字典形态**必须**在这里归一化：早期实现把 {"data": "close"} 当字面量塞进 bt 指标的
    #    位置参数，backtrader 会把它包成 LineSeriesStub，而该 stub 的 _clock 缺失、
    #    _owner 又指回指标自身 ⇒ Strategy._periodset（backtrader/strategy.py:168-180）
    #    上溯时钟链**死循环**：真跑回测表现为整轮卡住（不报错、不返回）。
    #    实测 {"func":"ma","args":[{"data":"close"},5]} 编译出的策略在最小 cerebro 里 100% 卡死，
    #    而 schema 里当时就是这么示范的（已同步改正为字符串写法）。
    resolved_args = []
    for index, arg in enumerate(raw_args):
        if isinstance(arg, dict):
            if 'data' in arg:
                data_name = str(arg.get('data') or 'close')
                if data_name in DATA_REF_MAP:
                    resolved_args.append(('data', data_name))
                else:
                    ctx.add_unsupported(f'指标 {func} 参数里的数据引用 {data_name} 未知，已回退 close')
                    resolved_args.append(('data', 'close'))
                if arg.get('shift'):
                    ctx.add_unsupported(f'指标 {func} 参数里的 shift 暂不支持，已忽略（回看写在规则左右值上）')
            elif 'value' in arg:
                resolved_args.append(('literal', arg.get('value')))
            elif 'param' in arg:
                param_name = str(arg.get('param'))
                if param_name in ctx.param_names:
                    resolved_args.append(('param', param_name))
                else:
                    ctx.add_unsupported(f'指标 {func} 参数引用了未声明的参数 {param_name}，已按 0 处理')
                    resolved_args.append(('literal', 0))
            else:
                ctx.add_gap('indicator_arg', str(sorted(arg)), f'指标 {func} 第 {index + 1} 个参数形态不支持')
                ctx.add_unsupported(f'指标 {func} 第 {index + 1} 个参数形态不支持（{sorted(arg)}），已按 close 处理')
                resolved_args.append(('data', 'close'))
            continue
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

    # ⚠️ 第一个参数必须是**数据引用**：literal / param 落在首位时绝不能当 data line 传给 bt ——
    # 非 LineRoot 参数会被 backtrader 包成 LineSeriesStub，其 _clock 缺失 + _owner 绕回自身 ⇒
    # Strategy._periodset 上溯时钟链死循环（整轮卡住、不报错；与字典形态同一个坑，
    # 实测 {"func":"ma","args":[0,5]} 会让回测挂死）。
    _arg_names = (INDICATOR_MAP.get(func) or {}).get('arg_names') or []
    if _arg_names and _arg_names[0] == 'data' and resolved_args and resolved_args[0][0] != 'data':
        ctx.add_unsupported(
            f'指标 {func} 的第一个参数必须是数据引用（如 "close"），'
            f'收到 {resolved_args[0]!r}，已按 close 处理')
        resolved_args[0] = ('data', 'close')

    return ctx.add_indicator(func, tuple(resolved_args), field)


def _resolve_data_ref(defn: dict, ctx: _CompileContext) -> str:
    """解析数据引用"""
    data_name = defn.get('data', 'close')
    try:
        shift = int(defn.get('shift', 0) or 0)
    except (TypeError, ValueError):
        ctx.add_unsupported(f'数据引用 shift 不是整数（{data_name} shift={defn.get("shift")}），已按当前 bar 处理')
        shift = 0
    if shift < 0:
        # 负数 shift 的语义是「未来 bar」＝前视偏差，一律拒绝并归零
        ctx.add_unsupported(f'数据引用 shift 不支持负数（{data_name} shift={shift}），已按当前 bar 处理')
        shift = 0

    if data_name not in DATA_REF_MAP:
        # ⛔ 绝不回退 close：未知/未实现字段如果被替换成收盘价，条件语义会
        # 完全变掉且体检看不出来（2026-10-20：pct_change >5 变成 close >5）。
        msg = f'未知或未实现的数据引用 {data_name}，该条件按恒假处理'
        logger.warning(msg)
        ctx.add_gap('data', data_name, '数据字段不在引擎能力表里')
        ctx.add_unsupported(msg)
        key = ctx.add_literal(0)
        ctx.bad_refs.add(key)
        return key

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


class _LookbackGuard(bt.Indicator):
    """把「裸 data 回看 N 根」并进 backtrader 的预热周期。

    指标各自的预热由 backtrader 自动统计（strategy._periodset 取所有指标
    _minperiod 的最大值），但 {"data": "close", "shift": 1} 这种裸数据回看
    它看不见。用一个空指标把回看根数表达出来，策略的 _minperiod 才会覆盖它，
    引擎（backtest/engine.py 的 min_bars）才能把「K 线不足以回看」判成
    insufficient_bars，而不是静默零交易。
    """

    lines = ('guard',)
    params = (('bars', 1),)

    def __init__(self):
        super().__init__()
        # addminperiod(N) 的语义是「本指标需要 N 根 K 线」
        # （LineSingle.addminperiod 内部是 _minperiod += N - 1，起点为 1），
        # 因此这里直接传所需的 bars，不要自己减 1。
        self.addminperiod(max(int(self.p.bars), 1))


def _build_strategy_class(
    ctx: _CompileContext,
    bt_params: tuple,
    class_name: str,
    strategy_name: str,
    buy_tree: RuleNode,
    sell_tree: RuleNode,
    hold_bars: int,
    params_raw: Dict[str, Any],
    chart_indicators: list,
    meta_description: str,
) -> Type[StockSolveStrategy]:
    """用 type() 动态生成策略类"""

    # ---- 闭包引用，避免 self 冲突 ----
    _ctx = ctx
    _buy_tree = buy_tree
    _sell_tree = sell_tree
    _hold_bars = int(hold_bars or 0)
    # hold_bars 下推给基类 params（基类统一实现倒计时与卖出口径），
    # on_bar 只保留「到点就卖」的短路判断，不再自己数 bar。
    if _hold_bars:
        bt_params = tuple(bt_params) + (('hold_bars', _hold_bars),)
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

        # 4. 数据回看预热：{"data": ..., "shift": N} 最大回看 N 根，
        #    需要 N+1 根 K 线才有值（shift=1 → 2 根，第 2 根起才读得到昨收）
        _max_shift = max(
            [int(info.get('shift') or 0) for info in _ctx.data_refs.values()
             if isinstance(info, dict)] + [0]
        )
        if _max_shift > 0:
            self._lookback_guard = _LookbackGuard(self.data, bars=_max_shift + 1)

    # ---- on_bar ----
    def on_bar(self):
        # scan 模式：无状态逐 bar 评估买卖条件，只记录命中、不下单
        # （不依赖 self.position，因此可在无持仓时评估 sell_tree）
        if self.p._scan_mode:
            buy_hit = _eval_rules(_buy_tree, self)
            sell_hit = _eval_rules(_sell_tree, self)
            hold_hit = bool(_hold_bars) and self.hold_bars_reached(_hold_bars)
            if buy_hit or sell_hit or hold_hit:
                self._signal_log.append({
                    'date': self.data.datetime.date(0).strftime('%Y-%m-%d'),
                    'buy': bool(buy_hit),
                    'sell': bool(sell_hit or hold_hit),
                    'hold_hit': hold_hit,
                    'bars_since_buy': self.bars_since_buy,
                })
            return
        # 默认模式：原交易逻辑
        if not self.position:
            if _eval_rules(_buy_tree, self):
                self.buy_with_reason(_make_reason(_buy_tree, self, '买入'))
            return
        # hold_bars 短路：持有满 N 根就无条件卖，不再等指标条件。
        # A股 T+1 下「尾盘买入、次日卖出」= hold_bars 1。
        if _hold_bars and self.hold_bars_reached(_hold_bars):
            self.sell_with_reason(f'持有 {_hold_bars} 根后卖出')
            return
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


#: 窗口类参数名（周期）：这些参数 < 1 会让 backtrader 内部除零
_WINDOW_ARG_NAMES = ('period', 'period_me1', 'period_me2', 'period_signal',
                     'period_dfast', 'short', 'long', 'smooth')


def _window_arg_name_at(func: str, index: int) -> str:
    """校验期用：args 下标 → 窗口参数名（不是窗口参数返回空串）。

    kdj 是特例：args 顺序是 (high, low, close, n)，与 INDICATOR_MAP 里的
    arg_names（data/period/period_dfast）不同位，周期在第 4 个位置。
    """
    if func == 'kdj':
        return 'period' if index == 3 else ''
    names = (INDICATOR_MAP.get(func) or {}).get('arg_names') or []
    name = names[index] if index < len(names) else ''
    return name if name in _WINDOW_ARG_NAMES else ''


def _create_indicator_instance(strategy_instance, func: str, bt_cls, args: tuple, kwargs: dict):
    """统一实例化入口：窗口类参数 < 1 一律按 1 处理并记账。

    backtrader 的窗口类指标内部是 sum/period 或 x/period（SMA / BollingerBands /
    MACD / RSI / Stochastic 全是），period=0 直接 ZeroDivisionError —— 而且异常冒到
    引擎会把**整轮**回测打成失败（实测 ma / rsi / boll / macd 四种全中）。
    0 只可能来自「参数引用未声明」的兜底或模型瞎写，把 0 当窗口没有任何意义：
    与其崩掉整轮，不如按 1 跑并在 unsupported 里如实记账。
    """
    fixed = dict(kwargs or {})
    for name in _WINDOW_ARG_NAMES:
        if name not in fixed:
            continue
        value = fixed[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value < 1:
            fixed[name] = 1
            note = f'指标 {func} 的参数 {name} 不能小于 1（收到 {value}），已按 1 处理'
            notes = getattr(strategy_instance, '_unsupported_conditions', None)
            if isinstance(notes, list) and note not in notes:
                notes.append(note)
    return bt_cls(*args, **fixed)


def _create_bt_indicator(strategy_instance, info: dict):
    """根据指标信息创建 Backtrader 指标实例"""
    func = info['func']
    field = info.get('field')
    args = info['args']

    # 解析 args
    resolved_args = []
    # 第一个 data 参数是不是 close 口径（决定要不要把它当位置参数传给 bt 指标）
    arg_type_of_first_is_close = bool(args) and args[0][0] == 'data' and str(args[0][1]) == 'close'
    for arg in args:
        arg_type = arg[0]
        arg_value = arg[1]
        if arg_type == 'param':
            resolved_args.append(getattr(strategy_instance.p, arg_value))
        elif arg_type == 'data':
            # close 口径传**数据源本身**（bt 的规范用法）：传裸线 self.data.close 会被包成
            # LineSeriesStub（lineseries.py:595-600 只设 _owner/_minperiod、不设 _clock），
            # 该 stub 上溯时钟链时落不到 data feed ⇒ 指标的预热周期（minperiod）统计不到
            # 这条线上，warmup 会失真。其余字段（high/low/volume）仍传线：bt 的标准用法。
            if arg_value == 'close':
                resolved_args.append(strategy_instance.data)
            else:
                resolved_args.append(getattr(strategy_instance.data, arg_value))
        elif arg_type == 'literal':
            resolved_args.append(arg_value)

    # ⚠️ 防御闸门：字典/数组形态的参数绝不能流到 backtrader ——
    # 非 LineRoot 的参数会被包成 LineSeriesStub，其 _clock 缺失 + _owner 绕回自身，
    # Strategy._periodset 上溯时钟链会死循环（整轮卡住，不报错）。宁可在这里报错让模型重试。
    for _arg in resolved_args:
        if isinstance(_arg, (dict, list, set)):
            raise ValueError(
                f'指标 {func} 的参数形态无法解析：{_arg!r}；参数只能是数值、'
                '"close" 这类数据名字符串、"{参数名}" 参数引用，'
                '或 {"data": "close"} / {"value": 数值} / {"param": "参数名"} 对象'
            )

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
        _kwargs = {'period': period, 'period_dfast': fixed_kwargs.get('period_dfast', 3)}
        # 其余 fixed_kwargs（如 safediv）必须一并透传：漏传会让 safediv 失效，
        # 一字板 / 平价窗口里 K = 100*(close-lowest)/(highest-lowest) 除零打挂整轮（实测）。
        _kwargs.update({k: v for k, v in fixed_kwargs.items() if k not in _kwargs})
        indicator = _create_indicator_instance(strategy_instance, func, bt_cls, (), _kwargs)
    # CYW: 同花顺主力控盘需要 high/low/close/volume，不传 data line，
    # 由 Backtrader 自动绑定到策略 data feed
    elif func == 'cyw':
        period = resolved_args[1] if len(resolved_args) > 1 else 10
        indicator = _create_indicator_instance(strategy_instance, func, bt_cls, (), {'period': period})
    # MACD: args 顺序 (close, fast, slow, signal) → BT 用 period_me1, period_me2, period_signal
    elif func == 'macd':
        kwargs = {
            'period_me1': resolved_args[1] if len(resolved_args) > 1 else 12,
            'period_me2': resolved_args[2] if len(resolved_args) > 2 else 26,
            'period_signal': resolved_args[3] if len(resolved_args) > 3 else 9,
        }
        indicator = _create_indicator_instance(strategy_instance, func, bt_cls, (resolved_args[0],), kwargs)
    else:
        # 通用构造: resolved_args[0] 是 data line（位置参数），其余是关键字参数
        # ⚠️ close 口径**不传 data line**，让 bt 自动绑定策略的 data feed ——
        # 与 kdj / cyw 分支同一写法（那两条在生产里一直正常）。
        # 原因：把 close 线当位置参数传进去时，bt 会包一个 LineSeriesStub，
        # 而该 stub 的 _clock 会绕回自己 → Strategy._periodset 上溯时钟链时死循环
        # （lineseries.py:595-600 不设 _clock + strategy.py:168-180 无环检测）。
        kwargs = dict(fixed_kwargs)
        for i, name in enumerate(arg_names):
            if i == 0:
                continue  # data line 走位置参数（close 口径例外，见上）
            if i < len(resolved_args):
                kwargs[name] = resolved_args[i]
        if arg_type_of_first_is_close:
            # 自动绑定策略 data feed（close 口径）
            indicator = _create_indicator_instance(strategy_instance, func, bt_cls, (), kwargs)
        else:
            indicator = _create_indicator_instance(strategy_instance, func, bt_cls,
                                                   (resolved_args[0],), kwargs)

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

    # 回看越过数据起点（如第一根 bar 上的「昨收」）：此刻条件无从成立。
    # 判假而不是抛异常 —— 异常会被 backtrader 打成整轮回测 error。
    if left_val is _UNAVAILABLE or right_val is _UNAVAILABLE:
        return False

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

    # 倍数包装：mul:<内层引用>:<倍数>
    if ref_key.startswith('mul:'):
        body = ref_key[4:]
        inner_key, _, factor_text = body.rpartition(':')
        try:
            factor = float(factor_text)
        except (TypeError, ValueError):
            return _UNAVAILABLE
        inner_val = _resolve_ref(inner_key, strategy_instance)
        if inner_val is _UNAVAILABLE:
            return _UNAVAILABLE
        try:
            return float(inner_val) * factor
        except (TypeError, ValueError):
            return _UNAVAILABLE

    # 字面量
    if ref_key.startswith('val_'):
        # 编译期标记的坏引用（引用了不存在的参数）：判假，不参与比较
        if ref_key in getattr(strategy_instance, '_bad_refs', ()):
            return _UNAVAILABLE
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
        # ⛔ 绝不回退 close：feed 没接这条线时必须判「不可用」，否则条件会
        # 静默变成「收盘价比较」（2026-10-20 排查的核心问题）。
        line = getattr(strategy_instance.data, data_name, None)
        if line is None:
            return _UNAVAILABLE
        if shift <= 0:
            value = line[0]
        else:
            # shift 语义 = N 根之前（1 = 昨收/昨日）。backtrader 的 line[i] 取的是第 i 个
            # 「未来」bar（line[-1] 才是前一根），早期实现写成 line[shift] 会造成前视偏差，
            # 并在最后一根 bar 上抛 IndexError: array index out of range。
            # idx 边界必须先判：idx=0 时 array[-1] 会回绕到数组末尾（Python 负索引）。
            idx = getattr(line, 'idx', None)
            if idx is not None and idx - shift < 0:
                return _UNAVAILABLE
            try:
                value = line[-shift]
            except IndexError:
                return _UNAVAILABLE
        # NaN/None 是「这一行没有值」，不是 0；返回 0 会让条件意外成立。
        if value is None:
            return _UNAVAILABLE
        try:
            if math.isnan(float(value)):
                return _UNAVAILABLE
        except (TypeError, ValueError):
            return _UNAVAILABLE
        return value

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


# ============================================================
# 编译期 field 校验（A3 Spike 修订 1）
# ============================================================


def _validate_fields(data: dict) -> None:
    """校验策略 JSON 中每个指标定义的 field 是否属于该指标的合法 field 列表。

    合法 field 来自 _fields_for_func()（macd→macd/signal、boll→upper/mid/lower、
    kdj→k/d/j、dpo→dpo/madpo、dma→dif/ama、cyw→cyw）；其余指标不允许带 field。
    """
    conditions = data.get('conditions') or {}

    def _walk(rules: list) -> None:
        for rule in rules or []:
            if not isinstance(rule, dict):
                continue
            if rule.get('type') == 'group':
                _walk(rule.get('rules', []))
                continue
            for side in ('left', 'right'):
                defn = rule.get(side)
                if not isinstance(defn, dict) or 'func' not in defn:
                    continue
                func = defn.get('func')
                field = defn.get('field')
                if not field or func not in INDICATOR_MAP:
                    continue
                valid_fields = {f['value'] for f in _fields_for_func(func)}
                if not valid_fields:
                    raise ValueError(
                        f"指标 '{func}' 不支持 field（单线指标不带 field），"
                        f"请移除 \"field\": \"{field}\"。"
                    )
                if field not in valid_fields:
                    raise ValueError(
                        f"指标 '{func}' 的 field '{field}' 不存在。\n"
                        f"合法 field: {sorted(valid_fields)}。"
                    )

    for side in ('buy', 'sell'):
        cond = conditions.get(side) or {}
        _walk(cond.get('rules', []))


_LEAF_RULE_TYPES = ('indicator', 'price', 'volume', 'pattern', 'time', 'custom')


def _validate_operand(defn: Any, where: str, errors: List[str]) -> None:
    if not isinstance(defn, dict) or not defn:
        errors.append(f'{where} 必须是非空对象（可用形态: value / func / data / param）')
        return
    if 'value' in defn:
        return
    if 'func' in defn:
        func = defn.get('func')
        if func == 'mul':
            # 倍数（operand 级特殊形态）：{"func":"mul","args":[<操作数>, 倍数]}
            # 两个参数是**操作数**而不是指标参数，因此走操作数校验，不走下面的 args 形态闸门。
            args = defn.get('args', [])
            if not isinstance(args, list) or len(args) != 2:
                errors.append(f'{where}.args 必须是 [<操作数>, 倍数] 两个元素')
            else:
                _validate_operand(args[0], f'{where}.args[0]', errors)
                factor = args[1]
                if isinstance(factor, bool) or not isinstance(factor, (int, float)):
                    errors.append(f'{where}.args[1] 必须是数值（倍数，如 1.5）')
            return
        if func not in INDICATOR_MAP:
            errors.append(f"{where}.func='{func}' 不是可用指标（可用: {', '.join(sorted(INDICATOR_MAP))}）")
        args = defn.get('args', [])
        if not isinstance(args, list):
            errors.append(f'{where}.args 必须是数组')
        else:
            # 参数形态闸门：数组/其它畸形形态必须在这里被拒 —— 字典形态早期会被当字面量
            # 塞进 bt 指标的位置参数，让回测**整轮卡死**（见 _resolve_indicator 的注释）。
            for index, arg in enumerate(args):
                where_arg = f'{where}.args[{index}]'
                if isinstance(arg, bool) or isinstance(arg, (int, float)):
                    continue
                if isinstance(arg, str):
                    continue  # "close" 数据名 / "{参数名}" / 其它字符串字面量，编译期归一化
                if isinstance(arg, dict):
                    if 'data' in arg:
                        name = arg.get('data')
                        if name not in DATA_REF_MAP:
                            errors.append(
                                f"{where_arg}.data='{name}' 不是可用数据引用（可用: {', '.join(sorted(DATA_REF_MAP))}）"
                            )
                    elif 'value' in arg or 'param' in arg:
                        continue
                    else:
                        errors.append(
                            f'{where_arg} 无法识别的参数形态 {sorted(arg)}'
                            '（可用: {"data": ...} / {"value": ...} / {"param": ...}）'
                        )
                    continue
                errors.append(
                    f'{where_arg} 形态不支持（可用: 数值 / "数据名" / "{{参数名}}" / '
                    '{"data": ...} / {"value": ...} / {"param": ...}）'
                )
            # 窗口类参数必须 >= 1：周期 0 会让 backtrader 内部除零、把整轮回测打挂
            # （见 _create_indicator_instance）。在保存期就拒收，让模型带原因重试。
            for index, arg in enumerate(args):
                name = _window_arg_name_at(func, index)
                if not name:
                    continue
                value = arg.get('value') if isinstance(arg, dict) else arg
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if value < 1:
                    errors.append(
                        f'{where}.args[{index}]（{name}）必须 >= 1（收到 {value}）：'
                        '周期为 0 的指标在回测里会直接除零'
                    )
        field = defn.get('field')
        if field is not None and field not in FIELD_MAP:
            errors.append(f"{where}.field='{field}' 不是可用字段（可用: {', '.join(sorted(FIELD_MAP))}）")
        return
    if 'data' in defn:
        data_name = defn.get('data')
        if data_name not in DATA_REF_MAP:
            errors.append(f"{where}.data='{data_name}' 不是可用数据引用（可用: {', '.join(sorted(DATA_REF_MAP))}）")
        return
    if 'param' in defn:
        if not isinstance(defn.get('param'), str):
            errors.append(f'{where}.param 必须是参数名字符串')
        return
    if 'pattern' in defn:
        errors.append(f'{where} 用了 pattern 条件：当前引擎不支持形态识别，该条件会被丢弃')
        return
    errors.append(f'{where} 无法识别的操作数形态 {sorted(defn)}（可用形态: value / func / data / param）')


def _validate_rule_list(rules: Any, path: str, errors: List[str], allow_empty: bool = False) -> None:
    if not isinstance(rules, list) or not rules:
        if not allow_empty:
            errors.append(f'{path} 必须是非空数组（没有条件就没有交易信号）')
        return
    valid_ops = COMPARE_OPS | CROSS_OPS
    for index, rule in enumerate(rules):
        where = f'{path}[{index}]'
        if not isinstance(rule, dict):
            errors.append(f'{where} 必须是对象')
            continue
        rule_type = rule.get('type', 'indicator')
        if rule_type == 'group':
            _validate_rule_list(rule.get('rules'), where + '.rules', errors)
            continue
        if rule_type not in _LEAF_RULE_TYPES:
            errors.append(f"{where}.type='{rule_type}' 不是支持的规则类型（可用: group, {', '.join(_LEAF_RULE_TYPES)}）")
            continue
        op = rule.get('op', '>')
        if op not in valid_ops:
            errors.append(f"{where}.op='{op}' 不是支持的运算符（可用: {', '.join(sorted(valid_ops))}）")
        _validate_operand(rule.get('left'), where + '.left', errors)
        _validate_operand(rule.get('right'), where + '.right', errors)


def validate_strategy_json(json_str: str) -> List[str]:
    """保存策略前的形状闸门：返回中文错误清单，空列表表示可编译

    与 _validate_fields 的分工：_validate_fields 管 field 白名单（编译期），
    本函数管结构完整性（保存期），必须跑在 compile_strategy 之前，
    否则畸形条件不会报错、只会静默变成恒假条件。
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return [f'JSON 解析失败: {e}']
    if not isinstance(data, dict):
        return ['策略 JSON 顶层必须是对象']
    conditions = data.get('conditions')
    if not isinstance(conditions, dict):
        return ['conditions 必须是对象，且包含 buy / sell 两块']
    errors: List[str] = []
    for side in ('buy', 'sell'):
        block = conditions.get(side)
        if not isinstance(block, dict):
            errors.append(f'conditions.{side} 必须是对象（含 logic 与 rules）')
            continue
        # ⚠️ 卖出侧允许空 rules：只要给了 hold_bars（「持有 N 根后卖出」），
        # 引擎就能表达纯时序卖点（compile_strategy 里就是这个口径）。
        # 早期这里一律要求非空，等于把「尾盘买入、次日卖出」这类**用户最常说的想法**
        # 挡在保存之外（真机复现：hold_bars=5 + sell.rules=[] 被判「没有卖点」）。
        has_hold = bool(block.get('hold_bars') or (conditions.get('hold_bars')))
        _validate_rule_list(block.get('rules'), f'conditions.{side}.rules', errors,
                            allow_empty=(side == 'sell' and has_hold))
    return errors


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
