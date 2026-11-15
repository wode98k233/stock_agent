"""池子级研究（cross-sectional study）：把「一只票的结论」升级成「一个池子的结论」。

为什么需要它（2026-10-18 用户质询）
------------------------------------
用户要的是研究员那种交付：「我用了 300 只标的、横跨 28 个行业、近一年，
整体成立；但在某某场景下胜率更高」。单标的研发只能回答「这只票上赚不赚钱」，
把它的胜率当成策略胜率没有统计意义。

研究设计（关键，别改坏）
------------------------
**选优集与验证集分离**：策略在单只票上调参收敛，再拿到全池上验证。
反过来（全池调参再全池验证）是典型的样本内过拟合。本模块只做验证，
不做选优，也不回写策略。

口径
----
- 有效 = 体检 ok 且成交笔数 >= MIN_TRADES 且胜率 > WIN_RATE_OK
- 弱   = 有结果但没达标
- 无法评判 = 体检非 ok / 笔数不足 / 异常（**不等于策略不行**）
- 中位数只在「可评判」的样本上算；分位数用线性插值
"""
from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MIN_TRADES = 5            # 与 MIN_SAMPLE 同口径
WIN_RATE_OK = 0.5         # 「有效」的胜率门槛
STUDY_WORKERS = 4         # 并发回测数（SQLite 写入压力与速度的折中）


@dataclass
class StockResult:
    """单只标的的验证结果。"""
    code: str
    name: str = ''
    health: str = ''
    trades: Optional[int] = None
    win_rate: Optional[float] = None
    total_return: Optional[float] = None
    sharpe: Optional[float] = None
    max_drawdown: Optional[float] = None
    profit_factor: Optional[float] = None   # 盈亏比（毛盈利/毛亏损）
    avg_hold_days: Optional[float] = None
    bars: Optional[int] = None
    neutral_rate: Optional[float] = None   # 落在中性带内的交易占比（解释「胜率不可用」）
    industry: str = ''
    error: str = ''

    @property
    def bucket(self) -> str:
        """valid / weak / unscored —— 三档口径。"""
        if self.error or self.health != 'ok':
            return 'unscored'
        if self.trades is None or self.trades < MIN_TRADES:
            return 'unscored'
        if self.win_rate is not None and self.win_rate > WIN_RATE_OK:
            return 'valid'
        return 'weak'


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _median(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(float(statistics.median(vals)), 4)


def _quantile(values: List[float], q: float) -> Optional[float]:
    """线性插值分位（q 在 0..1）。空列表返回 None。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return round(float(vals[0]), 4)
    pos = (len(vals) - 1) * max(0.0, min(1.0, q))
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return round(float(vals[lo] + (vals[hi] - vals[lo]) * frac), 4)


def summarize(results: List[StockResult]) -> Dict[str, Any]:
    """整体汇总：三档计数 + 可评判样本上的中位数与分位。"""
    total = len(results)
    buckets = {'valid': [], 'weak': [], 'unscored': []}
    for r in results:
        buckets[r.bucket].append(r)
    scored = buckets['valid'] + buckets['weak']

    wins = [_num(r.win_rate) for r in scored if _num(r.win_rate) is not None]
    rets = [_num(r.total_return) for r in scored if _num(r.total_return) is not None]
    sharpes = [_num(r.sharpe) for r in scored if _num(r.sharpe) is not None]
    pfs = [_num(r.profit_factor) for r in scored if _num(r.profit_factor) is not None]
    holds = [_num(r.avg_hold_days) for r in scored if _num(r.avg_hold_days) is not None]
    dds = [_num(r.max_drawdown) for r in scored if _num(r.max_drawdown) is not None]

    valid_n = len(buckets['valid'])
    return {
        'total': total,
        'valid': valid_n,
        'weak': len(buckets['weak']),
        'unscored': len(buckets['unscored']),
        'scored': len(scored),
        # 有效占比的分母用「可评判」而不是总数：数据缺失不该算进策略表现
        'valid_ratio': round(valid_n / len(scored), 4) if scored else None,
        'win_rate_median': _median(wins),
        'win_rate_p25': _quantile(wins, 0.25),
        'win_rate_p75': _quantile(wins, 0.75),
        'total_return_median': _median(rets),
        'sharpe_median': _median(sharpes),
        'profit_factor_median': _median(pfs),
        'avg_hold_days_median': _median(holds),
        'max_drawdown_median': _median(dds),
        'trades_median': _median([_num(r.trades) for r in scored if _num(r.trades) is not None]),
        'neutral_rate_median': _median([r.neutral_rate for r in scored if r.neutral_rate is not None]),
    }


def group_by_industry(results: List[StockResult]) -> List[Dict[str, Any]]:
    """按行业分组（只统计有行业映射的标的）。"""
    groups: Dict[str, List[StockResult]] = {}
    for r in results:
        key = (r.industry or '').strip() or '未分类'
        groups.setdefault(key, []).append(r)
    rows = []
    for name, items in groups.items():
        s = summarize(items)
        rows.append({
            'industry': name,
            'n': s['total'],
            'scored': s['scored'],
            'valid': s['valid'],
            'valid_ratio': s['valid_ratio'],
            'win_rate_median': s['win_rate_median'],
            'total_return_median': s['total_return_median'],
        })
    # 排序：先看有效占比，再看胜率中位；样本太小的排后面（n < 3 的结论没什么意义）
    rows.sort(key=lambda x: (x['scored'] >= 3, x['valid_ratio'] or -1, x['win_rate_median'] or -1),
              reverse=True)
    return rows


def verdict_of(overall: Dict[str, Any]) -> Dict[str, str]:
    """一句话结论。阈值写在代码里且可单测——不允许「看起来还行」这种模糊判定。

    成立     : 有效占比 >= 60% 且 胜率中位 > 50% 且 收益中位 > 0
    不成立   : 有效占比 < 30% 或 胜率中位 < 45% 或 收益中位 < 0
    有条件成立: 其余（典型形态：一半票能用，但整体不赚）
    """
    ratio = overall.get('valid_ratio')
    wr = overall.get('win_rate_median')
    ret = overall.get('total_return_median')
    if overall.get('scored', 0) < MIN_TRADES:
        return {'key': 'insufficient',
                'label': '样本不足',
                'headline': '可评判的标的太少（少于 5 只），这次池子研究说明不了问题，先补数据再跑。'}
    if ratio is None:
        return {'key': 'insufficient',
                'label': '样本不足',
                'headline': '没有足够可评判的标的，结论既不成立也不证伪。'}
    if wr is None:
        # 胜率算不出来（几乎全部交易落在 ±2% 中性带内）：只用收益与有效占比判，
        # 并在结论里明说「胜率不可用」，不能拿 0% 冒充。
        if ratio >= 0.6 and (ret or 0) > 0:
            return {'key': 'works', 'label': '整体成立（胜率不可用）',
                    'headline': '胜率无法计算（交易几乎全部落在 ±2% 中性带内），'
                                f'但 {int(round(ratio * 100))}% 的标的为正、收益中位 {(ret or 0):+.1%}。'}
        if ratio < 0.3 or (ret is not None and ret < 0):
            return {'key': 'fails', 'label': '整体不成立（胜率不可用）',
                    'headline': '胜率无法计算（交易几乎全部落在 ±2% 中性带内）；'
                                f'按收益看，只有 {int(round(ratio * 100))}% 的标的达到「有效」、'
                                f'收益中位 {(ret or 0):+.1%} —— 结论是不成立。'}
        return {'key': 'conditional', 'label': '有条件成立（胜率不可用）',
                'headline': '胜率无法计算（中性带吃掉了全部交易）；按收益看结论不明确。'}
    if ratio >= 0.6 and wr > 0.5 and (ret or 0) > 0:
        return {'key': 'works', 'label': '整体成立',
                'headline': ('在 ' + str(int(round(ratio * 100))) + '% 的标的上有正胜率、'
                             '胜率中位 ' + format(wr, '.0%') + '、收益中位 '
                             + format(ret, '+.1%') + ' —— 这个形态是站得住的。')}
    if ratio < 0.3 or wr < 0.45 or (ret is not None and ret < 0):
        return {'key': 'fails', 'label': '整体不成立',
                'headline': ('只有 ' + str(int(round(ratio * 100))) + '% 的标的达到「有效」，'
                             '胜率中位 ' + format(wr, '.0%') + '、收益中位 '
                             + format((ret or 0), '+.1%')
                             + ' —— 结论是「这个想法在当前形态下不成立」，不是没跑。')}
    return {'key': 'conditional', 'label': '有条件成立',
            'headline': ('在 ' + str(int(round(ratio * 100))) + '% 的标的上有效，胜率中位 '
                         + format(wr, '.0%') + '、收益中位 ' + format((ret or 0), '+.1%')
                         + ' —— 不是普适策略，但值得缩小场景再研究。')}


def build_caveats(*, total: int, scored: int, with_industry: int,
                  period_days: int, bar_median: Optional[int],
                  neutral_rate_median: Optional[float] = None) -> List[str]:
    """必须随报告一起给出的局限——研究结论没有边界就是误导。"""
    out = [
        '验证集与选优集分离：策略是在单只标的上调参收敛的，本报告是它在 '
        + str(total) + ' 只标的上的**样本外**表现。',
        '成分股用的是**当期快照**，不是历史成分：存在幸存者偏差（退市/被剔除的票不在样本里）。',
    ]
    if bar_median is not None and bar_median < 250:
        out.append('日线覆盖偏短（中位 ' + str(bar_median) + ' 根），样本不足一年，结论强度有限。')
    if period_days and period_days < 400:
        out.append('区间只有 ' + str(period_days) + ' 天，未覆盖完整的牛熊切换，跨市场状态的泛化性未知。')
    if neutral_rate_median is not None and neutral_rate_median > 0.8:
        out.append('有 ' + format(neutral_rate_median, '.0%') + ' 的交易收益落在 ±2% 中性带内'
                   '（策略是 1 日持有，日波动本来就小）—— 中性带对这类策略几乎没有区分度，'
                   '主口径胜率仍按「盈利笔数 / 总笔数」计算，但「中性带调整后胜率」不可用。')
    if scored < total * 0.8:
        out.append(str(total - scored) + ' 只标的无法评判（数据/样本不足），不等于策略在它们身上不行。')
    if with_industry < total * 0.8:
        out.append('行业映射只覆盖 ' + str(with_industry) + '/' + str(total)
                   + ' 只，行业分组的代表性有限。')
    return out


def run_study(*, codes: List[Dict[str, str]], run_segment: Callable[[str], Dict[str, Any]],
              industries: Optional[Dict[str, str]] = None,
              progress: Optional[Callable[[int, int, int, int], None]] = None,
              workers: int = STUDY_WORKERS) -> List[StockResult]:
    """并发跑全池，逐只收集结果（单只失败不影响整批）。

    参数 run_segment(code) 由调用方注入（回测引擎在 server 层，本模块保持可单测）。
    参数 progress(done, total, ok, fail) 每完成一只回调一次。
    """
    industries = industries or {}
    total = len(codes)
    results: List[StockResult] = []
    ok = fail = 0
    done = 0

    def _one(item: Dict[str, str]) -> StockResult:
        code = item['code']
        r = StockResult(code=code, name=item.get('name', ''),
                        industry=industries.get(code, ''))
        try:
            m = run_segment(code) or {}
            r.health = str(m.get('health') or '')
            r.trades = int(m['trades']) if m.get('trades') is not None else None
            # win_rate 脏值防护，与 strategy_research.clamp_win_rate 同一口径：
            # 中性交易剔除后有效样本为 0 时会产出 >1 的脏值（实测 10.0）。
            # clamp 到 1 会误导，置空更诚实 —— 置空后该票只能落到「弱」，不会算进「有效」。
            wr = _num(m.get('win_rate'))
            r.win_rate = wr if (wr is not None and 0 <= wr <= 1) else None
            r.total_return = _num(m.get('total_return'))
            r.sharpe = _num(m.get('sharpe'))
            r.bars = int(m['data_bars']) if m.get('data_bars') is not None else None
            nr = _num(m.get('neutral_rate'))
            r.neutral_rate = nr if (nr is not None and 0 <= nr <= 1) else None
        except Exception as e:  # noqa: BLE001 - 单只失败不拖垮整批
            r.health = 'error'
            r.error = str(e)[:160]
        return r

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for r in pool.map(_one, codes):
            results.append(r)
            done += 1
            if r.bucket == 'unscored' and r.error:
                fail += 1
            else:
                ok += 1
            if progress:
                try:
                    progress(done, total, ok, fail)
                except Exception:  # noqa: BLE001
                    pass
    return results
