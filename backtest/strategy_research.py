"""A3 策略研发闭环 —— 循环状态机核心（T07）

把「自然语言想法」变成「经 OOS 验证的策略」：
    idea → [GEN] LLM 生成策略+假设 → [COMPILE] json_compiler 校验
      → 失败 → 错误反馈重试(≤2) → 仍失败 → failed(兜底文案)
      → 成功 → [BACKTEST] engine 跑 IS+OOS → [EVAL] 指标摘要+红线判定
      → [DECIDE] LLM 读全部轮次历史 → 变异假设 → 下一轮
      → 终止(连续2轮OOS未升 / 8轮 / 预算) → [REPORT] 最优策略卡

Spike 修订（docs/spikes/2026-09-06-t00-sp1-compile-rate.md §4，全部落实）：
1. 编译期 field 校验在 json_compiler 内（本模块 compile 前即拦截非法 field）；
2. LLM 调用内建限流节奏：调用间隔 ≥20s、429 退避 30s（实测无间隔 12/20 熔断）；
3. OOS 短样本保护：OOS trades<5 的轮次标记 low_sample，不参与最优夏普比较；
4. win_rate 脏值防护：落库前对 win_rate 做 [0,1] 校验，越界 None 化；
5. 切分日口径：OOS 起点取切分日次一交易日（避免 IS 尾日与 OOS 首日重复计价）。

纯逻辑层：LLM（StrategyGenerator）与回测（IsoOsRunner）均可注入替身，单测友好。
"""
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# 常量（状态机与红线参数）
# ============================================================

MAX_ROUNDS = 8                 # 轮次硬上限（AC-6：第 8 轮后强制终止）
OVERFIT_RATIO = 1.3            # IS/OOS 夏普比红线（G3）
OOS_MIN_TRADES = 5             # OOS 短样本保护阈值（Spike §2.3：短样本夏普剧烈波动）
IS_MIN_TRADES = 5              # IS 短样本保护阈值（与 OOS 同口径：5 笔以内不参与比较）
CONVERGED_STREAK = 2           # OOS 夏普连续 N 轮未提升 → 收敛终止（AC-5）
LLM_PACING_SECONDS = 20.0      # LLM 调用最小间隔（Spike 第二轮实测参数）
LLM_BACKOFF_SECONDS = 30.0     # 429/限流退避秒数
MAX_GEN_RETRIES = 2            # 非法输出的错误反馈重试上限（初次 + 2 次重试 = 3 次机会）

DISCLAIMER = '历史表现不代表未来收益，本结果仅为研究结论，不构成投资建议'

# 文案红线敏感词（AC-8）：prompt 指令禁用 + 输出扫描过滤
SENSITIVE_WORDS: Tuple[str, ...] = ('稳赚', '必胜', '躺赢', '包赚', '稳赢')

# 盘口概念词：命中时追加「深V/振幅」的价格条件表达提示（见 _concept_note）
# ⚠️ 这**不是**阶段路由用的关键词表（策略会话阶段判定归 LLM），
# 只用于 prompt 里的条件写法映射；命中与否都不会改变「是不是策略研究」。
_DIP_CONCEPT_WORDS: Tuple[str, ...] = (
    '深v', '深 v', '超跌', '大跌', '急跌', '暴跌', '下探', '探底', '深蹲',
    '反弹', '振幅', '收回', '长下影',
)

# 终止原因（strategy_run.stop_reason）
STOP_CONVERGED = 'converged'
STOP_MAX_ROUNDS = 'max_rounds'
STOP_BUDGET = 'budget'
STOP_FAILED = 'failed'
STOP_CANCELLED = 'cancelled'

#: 回测**执行失败**（引擎/数据/指标异常，不是策略效果差）连续达到这个轮数才终止整次研发。
#: 单轮崩掉不该带走整个 8 轮任务：把真实报错回灌给模型，换一轮重试。
#: 真机事故（2026-10-14）：第 4 轮 RSI 周期 2 触发 backtrader 除零，一次研发直接 failed。
MAX_BACKTEST_FAILURES = 3

# 全局 LLM 调用节奏（跨 Generator 实例共享，保证多轮循环串行调用时也遵守间隔）
_llm_pacing_lock = threading.Lock()
_last_llm_ts: float = 0.0


# ============================================================
# 数据充足性前置闸门（真机事故 2026-10-18）
# ============================================================
# 事故复盘：universe=000300 被当作股票，stock_daily 里只有 24 根**月线**（mx_data 返回的
# 月频快照），却因为「行数 > 20」通过了补数校验。于是 8 轮研发全部跑在 16 根 bar 上：
# 轮1-3 零成交（minperiod 20）、轮7 用 5 笔交易报出 383% 年化。模型拿不到「没数据」这个
# 事实，8 轮都在「条件太严/太松」上反复试错，烧完全部预算。
#
# 所以在**第一轮 LLM 调用之前**先验数据：条数够不够、是不是日线。不达标直接判 failed，
# 不占用 8 轮预算、也不让模型去猜。

PREFLIGHT_MIN_BARS = 60              # 绝对下限：少于 60 根无法评估任何指标策略
PREFLIGHT_MAX_DAILY_GAP_DAYS = 5.0   # 相邻 bar 间隔中位数 > 5 天 ⇒ 不是日线


def _asset_type_of(code: str) -> str:
    """识别标的资产类型：复用批量回测的既有规则，避免两套前缀口径漂移。

    规则来源：``backtest/batch_engine.py::_auto_detect_asset_type``
    （51/52/56/58/15/16/18 开头 → etf；399 开头 → index；其余 → stock）。
    函数内延迟 import：batch_engine 模块级就 import ``backtest.engine``（backtrader
    全家桶），研发链路的纯逻辑单测不该为一次识别付这份导入成本。
    """
    from backtest.batch_engine import _auto_detect_asset_type
    return _auto_detect_asset_type(code)


def _median_gap_days(df) -> Optional[float]:
    """相邻 trade_date 间隔的中位数（自然日）。不足两根返回 None。"""
    if df is None or len(df) < 2:
        return None
    try:
        import pandas as pd
        days = pd.to_datetime(df['trade_date']).sort_values().diff().dt.days.dropna()
        return float(days.median()) if len(days) else None
    except Exception:  # noqa: BLE001 - 日期列异常时不做频率判定，交给条数判定
        return None


def preflight_data(code: str, start: str, end: str) -> Dict[str, Any]:
    """回测前的数据充足性校验；不达标抛 ``ValueError``（文案可直接给用户看）。

    只读本地库，**不外呼**（补数是数据层的职责，不在这里做——否则又会让
    「拉到什么就用什么」的静默降级重演）。

    Returns:
        {'bars', 'required', 'gap_days', 'span_days', 'actual_start', 'actual_end'}

    ``required`` 按**实际首末 bar 的自然日跨度**算，不按请求区间：
    请求 3 年但标的 2024 年才上市（300 根 / 约 700 天）时 required≈233 根 → 能过；
    若按请求区间（1095/3=365）会把次新股误杀。
    """
    from datetime import datetime as _dt

    from utils.cache.market_data_db import get_stock_daily

    df = get_stock_daily(code, start_date=start, end_date=end)
    bars = 0 if df is None or df.empty else int(len(df))

    actual_start = actual_end = ''
    span_days = 0
    if bars:
        try:
            import pandas as pd
            dates = pd.to_datetime(df['trade_date']).sort_values()
            actual_start = dates.iloc[0].strftime('%Y-%m-%d')
            actual_end = dates.iloc[-1].strftime('%Y-%m-%d')
            span_days = max(int((dates.iloc[-1] - dates.iloc[0]).days), 0)
        except Exception:  # noqa: BLE001 - 日期不可解析时退回请求区间
            span_days = 0
    if span_days <= 0:
        try:
            span_days = (_dt.strptime(end[:10], '%Y-%m-%d')
                         - _dt.strptime(start[:10], '%Y-%m-%d')).days
        except Exception:  # noqa: BLE001
            span_days = 0

    required = max(PREFLIGHT_MIN_BARS, span_days // 3)
    gap = _median_gap_days(df)

    reasons: List[str] = []
    if bars < required:
        reasons.append(f'只有 {bars} 根 K 线（需 ≥{required} 根）')
    # ETF 的在线兜底在 data_adapter（股票接口无数据时改走 ETF 接口），而本闸门
    # 只读本地库、不外呼 —— 本地没数据的 ETF 会被笼统判成「数据不足」。这里给出
    # 可执行的提示；**不在闸门里联网补拉**（补数是数据层的职责）。
    if bars == 0 and _asset_type_of(code) == 'etf':
        reasons.append('本地库无该 ETF 日线，可先补拉 ETF 日线后再跑（预检不联网补拉）')
    if gap is not None and gap > PREFLIGHT_MAX_DAILY_GAP_DAYS:
        reasons.append(f'相邻间隔中位数 {gap:.0f} 天（非日线）')
    if reasons:
        raise ValueError(
            f'标的 {code} 在 {start}~{end} ' + '，且'.join(reasons) + '。请检查数据源。')
    return {'bars': bars, 'required': required, 'gap_days': gap,
            'span_days': span_days, 'actual_start': actual_start,
            'actual_end': actual_end}


def short_span_note(info: Dict[str, Any], start: str, end: str) -> str:
    """实际行情区间短于请求区间时给模型的一句说明（不能静默缩短）。

    只在**数据够用**（闸门已通过）时调用：告诉模型「结论只覆盖这段」，
    避免它把「缺的那段没有信号」当成策略特性。
    """
    a_start = str((info or {}).get('actual_start') or '')
    a_end = str((info or {}).get('actual_end') or '')
    if not a_start or not a_end:
        return ''
    if a_start > start[:10] or a_end < end[:10]:
        return (f'实际可用行情 {a_start}~{a_end}（{info.get("bars")} 根），'
                f'短于请求区间 {start}~{end}：结论只覆盖实际区间，'
                f'不要假设缺失段有数据，也不要把「缺口段没触发」当成策略特性。')
    return ''


#: 体检索引值 → 给模型看的人话（与 backtest/health.py 的常量集一致）
HEALTH_LABELS: Dict[str, str] = {
    'ok': '正常',
    'no_signal': '零成交',
    'insufficient_bars': 'K线不足指标预热',
    'data_gap': '数据缺口',
    'bad_strategy': '条件引擎表达不了',
    'invalid_params': '参数不成立',
    'error': '执行出错',
}


def _health_label(health: Any) -> str:
    h = str(health or '').strip().lower()
    return HEALTH_LABELS.get(h, h or '未记录')


def _bars_text(metrics: Dict[str, Any]) -> str:
    """「数据：16/615 根(2.6%)」——让模型看得见样本规模，而不是只看到收益数字。"""
    bars = (metrics or {}).get('data_bars')
    if bars is None:
        return '未记录'
    expected = (metrics or {}).get('expected_bars')
    cov = (metrics or {}).get('coverage_pct')
    cov_txt = f'{float(cov) * 100:.1f}%' if isinstance(cov, (int, float)) else '—'
    return f'{bars}/{expected if expected is not None else "?"} 根({cov_txt})'


def _summary_line(it: "IterRecord") -> str:
    """上下文压缩时「每轮 1 行」的摘要。"""
    is_m = it.is_metrics or {}
    oos_m = it.oos_metrics or {}
    hyp = (it.hypothesis or '（无）').replace('\n', ' ')[:60]
    return (f'- 第{it.round}轮 假设:{hyp} | IS sharpe={is_m.get("sharpe")}, '
            f'trades={is_m.get("trades")} | OOS sharpe={oos_m.get("sharpe")}, '
            f'trades={oos_m.get("trades")} | 体检:{_health_label(is_m.get("health"))} '
            f'| 判定:{it.verdict}')


def _extract_llm_meta(resp: Any, label: str = '', model: str = '') -> Dict[str, Any]:
    """从一次 LLM 响应里取 usage 与前缀指纹（拿不到一律 0/空串，绝不抛）。

    provider 的 prompt cache 是「从头连续前缀」的字节匹配：``prefix_sha256`` 只要跨轮
    不变，就说明消息列表是真的 append-only；变了就说明中间有非确定性内容（时间戳、
    排序、重新渲染）。这是排查「为什么 cached_tokens 还是 0」的第一手证据。
    """
    meta: Dict[str, Any] = {
        'label': label, 'model': model, 'input_tokens': 0, 'output_tokens': 0,
        'cached_tokens': 0, 'prefix_sha256': '', 'prefix_bytes': 0,
    }
    try:
        um = getattr(resp, 'usage_metadata', None) or {}
        if isinstance(um, dict):
            meta['input_tokens'] = int(um.get('input_tokens') or 0)
            meta['output_tokens'] = int(um.get('output_tokens') or 0)
            details = um.get('input_token_details') or {}
            if isinstance(details, dict):
                meta['cached_tokens'] = int(details.get('cache_read') or 0)

        rm = getattr(resp, 'response_metadata', None) or {}
        if isinstance(rm, dict):
            tu = rm.get('token_usage') or rm.get('usage') or {}
            if isinstance(tu, dict):
                meta['input_tokens'] = (meta['input_tokens']
                                        or int(tu.get('prompt_tokens') or 0))
                meta['cached_tokens'] = (meta['cached_tokens'] or int(
                    tu.get('prompt_cache_hit_tokens') or tu.get('cached_tokens') or 0))
            llm_out = rm.get('llm_output') if isinstance(rm.get('llm_output'), dict) else {}
            fp = rm.get('prefix_sha256') or (llm_out or {}).get('prefix_sha256') or ''
            size = rm.get('prefix_bytes') or (llm_out or {}).get('prefix_bytes') or 0
            if fp:
                meta['prefix_sha256'] = str(fp)
                try:
                    meta['prefix_bytes'] = int(size or 0)
                except (TypeError, ValueError):
                    meta['prefix_bytes'] = 0
    except Exception:  # noqa: BLE001 - 观测字段，拿不到不影响生成
        pass
    return meta


def is_off_intent(it: "IterRecord") -> bool:
    """该轮实现**明确偏离用户原话**（核对结果是 checked 且 matches=False）。

    fail-open：未核对（开关关 / 核对 LLM 挂）不算偏离 —— 核对不了不能当红的用。
    「赚不赚钱」与「是不是我说的那个策略」是两件事：把「上涨买入」实现成
    「下跌反弹买入」的高夏普轮次，对用户是**错误答案**。
    """
    ic = (it.intent_check or {}) if it is not None else {}
    return bool(ic.get('checked')) and ic.get('matches') is False


def round_is_degenerate(is_metrics: Optional[Dict[str, Any]],
                        oos_metrics: Optional[Dict[str, Any]]) -> bool:
    """这一轮是否「什么都没测到」：零成交，或体检不是 ok。

    这类轮次的 cagr/sharpe 没有信息量（0 笔算出 0%，月线算出 383%），既不能当选最优，
    也不该以 `keep` 出现在轮次表里。health 缺失（老数据/替身 runner）时只看成交笔数，
    保持向后兼容。
    """
    def _trades(m):
        # 缺失 ≠ 零成交：老记录可能只有 sharpe 没有 trades，按「未知」放行
        if not m:
            return None
        v = m.get('trades')
        return int(v) if isinstance(v, (int, float)) else None

    if _trades(is_metrics) == 0 or _trades(oos_metrics) == 0:
        return True
    for m in (is_metrics, oos_metrics):
        health = str((m or {}).get('health') or '').strip().lower()
        if health and health != 'ok':
            return True
    return False


# ============================================================
# 数据结构
# ============================================================


@dataclass
class GenResult:
    """LLM 生成结果（GEN/DECIDE 节点输出）。"""
    ok: bool
    strategy_json: Optional[str]      # 策略全文 JSON 字符串
    hypothesis: str                   # agent 本轮假设
    error: Optional[str] = None       # 用户可读原因，不暴露堆栈
    model: str = ''                   # 实际使用的模型名（可追溯）
    attempts: int = 1                 # 实际尝试次数


@dataclass
class IterRecord:
    """单轮迭代记录（strategy_iter 表的内存形态）。"""
    round: int
    hypothesis: str = ''
    strategy_json: Optional[str] = None
    strategy_hash: Optional[str] = None
    strategy_diff: str = ''
    compile_ok: int = 0
    compile_error: Optional[str] = None
    is_metrics: Optional[Dict[str, Any]] = None
    oos_metrics: Optional[Dict[str, Any]] = None
    # 意图保真核对结果（backtest/intent_fidelity.py）：checked/matches/deviations
    intent_check: Optional[Dict[str, Any]] = None
    overfit_flag: int = 0
    low_sample: int = 0               # OOS trades < OOS_MIN_TRADES（不参与最优比较）
    verdict: str = ''                 # keep / reject / best / failed
    # 失败来源：gen（生成侧连续非法，终止） / backtest（回测执行异常，可换轮重试）
    failure_kind: str = ''
    model: str = ''
    id: Optional[int] = None
    created_at: str = ''

    def oos_sharpe(self) -> Optional[float]:
        """OOS 夏普（缺失/非数值返回 None）。"""
        if not self.oos_metrics:
            return None
        v = self.oos_metrics.get('sharpe')
        return float(v) if isinstance(v, (int, float)) else None

    def is_sharpe(self) -> Optional[float]:
        if not self.is_metrics:
            return None
        v = self.is_metrics.get('sharpe')
        return float(v) if isinstance(v, (int, float)) else None


@dataclass
class RunResult:
    """整次研究任务的结果（strategy_run 表的终态）。"""
    run_uuid: str
    status: str                       # done / failed / cancelled
    stop_reason: str                  # converged / max_rounds / budget / failed / cancelled
    total_iters: int = 0
    best_iter: Optional[IterRecord] = None


# ============================================================
# 敏感词与脏值防护（Spike 修订 4 + AC-8）
# ============================================================


def scan_sensitive_words(text: str) -> List[str]:
    """扫描文本中的敏感词，返回命中列表（有序去重）。"""
    if not text:
        return []
    hits: List[str] = []
    for w in SENSITIVE_WORDS:
        if w in text and w not in hits:
            hits.append(w)
    return hits


def sanitize_text(text: str) -> str:
    """把敏感词替换为中性表述（'稳赚策略'→'策略'），供 hypothesis/meta 清洗。"""
    if not text:
        return text or ''
    cleaned = text
    for w in SENSITIVE_WORDS:
        cleaned = cleaned.replace(w, '')
    return cleaned


def sanitize_strategy(obj: Dict[str, Any]) -> Dict[str, Any]:
    """清洗策略 JSON 的 meta 文本字段（name/description/tags），就地无效，返回新对象。"""
    meta = dict(obj.get('meta') or {})
    meta['name'] = sanitize_text(str(meta.get('name', '')))
    meta['description'] = sanitize_text(str(meta.get('description', '')))
    if isinstance(meta.get('tags'), list):
        meta['tags'] = [sanitize_text(str(t)) for t in meta['tags']]
    out = dict(obj)
    out['meta'] = meta
    return out


def clamp_win_rate(metrics: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """win_rate 脏值防护（Spike 修订 4）。

    既有 _calculate_stats 在中性交易剔除后有效样本为 0 时会产出 >1 的脏值（如 3.0）。
    规则：win_rate 为 None 保留 None；落在 [0,1] 保留（四位舍入）；越界 → None 化
    （3.0 这类脏值 clamp 到 1.0 会误导，直接置空更诚实）。
    """
    if metrics is None:
        return None
    out = dict(metrics)
    wr = out.get('win_rate')
    if wr is None:
        out['win_rate'] = None
    elif isinstance(wr, (int, float)):
        if wr < 0 or wr > 1:
            out['win_rate'] = None
        else:
            out['win_rate'] = round(float(wr), 4)
    else:
        out['win_rate'] = None
    return out


# ============================================================
# JSON 提取与 diff
# ============================================================


def extract_json_block(raw: str) -> Optional[Dict[str, Any]]:
    """从 LLM 原始输出提取 JSON 对象（容忍 markdown 围栏与前后杂文）。Spike 已验证。"""
    if not raw:
        return None
    text = raw.strip()
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    candidates: List[str] = []
    if m:
        candidates.append(m.group(1))
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def strategy_hash(strategy_json: str) -> str:
    """规范化策略 JSON 的 sha1（IS/OOS 结果缓存键 + 落库）。"""
    try:
        canonical = json.dumps(json.loads(strategy_json), sort_keys=True, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        canonical = strategy_json or ''
    return hashlib.sha1(canonical.encode('utf-8')).hexdigest()[:16]


def diff_strategy(prev_json: Optional[str], new_json: Optional[str]) -> str:
    """生成上一轮 → 本轮的可读差异摘要（params 数值 + 结构变化）。"""
    if not prev_json or not new_json:
        return '首轮生成'
    try:
        prev = json.loads(prev_json)
        new = json.loads(new_json)
    except json.JSONDecodeError:
        return '结构变化（上一轮 JSON 不可解析）'
    parts: List[str] = []

    prev_params = prev.get('params') or {}
    new_params = new.get('params') or {}
    for key, info in new_params.items():
        new_val = info.get('value') if isinstance(info, dict) else info
        prev_info = prev_params.get(key)
        prev_val = prev_info.get('value') if isinstance(prev_info, dict) else prev_info
        if prev_val != new_val:
            parts.append(f'参数 {key}: {prev_val} → {new_val}')
    for key in prev_params:
        if key not in new_params:
            parts.append(f'移除参数 {key}')

    def _summarize_rules(strategy: dict) -> List[str]:
        conds = strategy.get('conditions') or {}
        out = []
        for side in ('buy', 'sell'):
            rules = (conds.get(side) or {}).get('rules') or []
            funcs = []
            for r in rules:
                for lr in ('left', 'right'):
                    d = (r.get(lr) or {}) if isinstance(r, dict) else {}
                    if isinstance(d, dict) and d.get('func'):
                        funcs.append(d['func'] + (f":{d['field']}" if d.get('field') else ''))
            out.append(f'{side}[{len(rules)}条: {",".join(funcs) if funcs else "-"}]')
        return out

    prev_sig = _summarize_rules(prev)
    new_sig = _summarize_rules(new)
    if prev_sig != new_sig:
        parts.append('结构 ' + ' / '.join(new_sig))

    if not parts:
        return '无结构差异（参数与规则未变）'
    return '；'.join(parts[:8])


# ============================================================
# LLM 策略生成器
# ============================================================


class StrategyGenerator:
    """LLM 策略生成/变异（走 utils.llm 网关）。

    llm 可注入替身（单测）；缺省惰性取 gateway.chat('report')（Spike 同用途）。

    **append-only 多轮上下文**（2026-10-19 实测）：该 provider 的 prompt cache 只认
    「同一前缀 + 尾部追加」——改 user 中段、改 system 一个字节、完全重发都是 0 命中。
    react_stock 靠 append-only 做到 69.3%，而本模块每轮从零重建 [system, user] 只有
    31.2%（且全来自重试）。所以 self._messages 跨轮持有：首轮 [system, user]，
    每轮只追加 (user=上一轮结果, assistant=模型原文)。
    """

    MAX_CONTEXT_CHARS = 60000     # 超过就压缩（字符数近似 token）
    MAX_CONTEXT_ROUNDS = 10       # 或轮次超过这个数
    KEEP_TAIL_MESSAGES = 5        # 压缩时保留：最近 2 轮的 user/assistant + 本轮 prompt
    ASSISTANT_RAW_LIMIT = 8000    # 模型原文入上下文的截断上限（异常长输出不撑爆前缀）

    def __init__(self, llm: Any = None,
                 pacing_seconds: float = LLM_PACING_SECONDS,
                 backoff_seconds: float = LLM_BACKOFF_SECONDS,
                 max_retries: int = MAX_GEN_RETRIES,
                 llm_logger: Any = None,
                 entry_timing: str = 'next_open', exit_timing: str = '',
                 pool_mode: bool = False, pool_desc: str = ''):
        self._llm = llm
        # append-only 上下文（跨轮持有；不要每轮重建）
        self._messages: List[Tuple[str, str]] = []
        self._round_no = 0
        self._compressed = False
        self._last_llm_meta: Dict[str, Any] = {}
        self._usage_log: List[Dict[str, Any]] = []
        self.pacing_seconds = pacing_seconds
        self.backoff_seconds = backoff_seconds
        self.max_retries = max_retries
        self.logger = llm_logger or logger
        # 成交口径：必须告知 LLM，否则它会自己编条件去「表达」尾盘买入
        self.entry_timing = entry_timing
        self.exit_timing = exit_timing or entry_timing
        # 池子模式：不要按 pilot 单票的涨跌停上限写死条件（混合池里有 10%/20%/ETF）
        self.pool_mode = bool(pool_mode)
        self.pool_desc = str(pool_desc or '')

    # ── LLM 句柄 ──

    def _get_llm(self):
        if self._llm is None:
            import config  # noqa: F401  # 加载 .env，必须先于 utils.llm
            from utils.llm import gateway
            self._llm = gateway.chat('report')
        return self._llm

    def _pacing(self, extra: float = 0.0) -> None:
        """调用间隔节奏（全局共享；Spike 修订 2：每轮 LLM 调用间隔 ≥ pacing）。"""
        global _last_llm_ts
        wait = self.pacing_seconds + extra
        with _llm_pacing_lock:
            elapsed = time.time() - _last_llm_ts
            if elapsed < wait:
                time.sleep(wait - elapsed)
            _last_llm_ts = time.time()

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        """识别 429/限流类异常（退避重试，不消耗编译重试预算）。"""
        text = f'{type(exc).__name__}: {exc}'
        return any(k in text for k in ('429', 'RateLimited', 'rate limit', 'RateLimit',
                                       'Too Many Requests', 'quota', 'Quota'))

    def _invoke_llm(self, messages: List[Tuple[str, str]], label: str = '') -> str:
        """带节奏与 429 退避的 LLM 调用。返回原始文本；失败抛出异常。

        ``config.metadata`` 用于 trace 归因（purpose=strategy-generate，label=第N轮-生成）；
        ``prefix_sha256`` / ``prefix_bytes`` 由 provider 侧在前缀指纹计算后回填，这里读到就
        记进 ``_usage_log``（供「缓存到底有没有命中」的排查，不参与业务判断）。
        """
        llm = self._get_llm()
        lc_messages = _to_langchain_messages(messages)
        metadata = {'purpose': 'strategy-generate',
                    'label': label or 'strategy-generate'}
        last_exc: Optional[Exception] = None
        for attempt in range(3):  # 网关层之上最多 3 次（含退避）
            self._pacing(extra=self.backoff_seconds if attempt else 0.0)
            try:
                try:
                    resp = llm.invoke(lc_messages, config={'metadata': metadata})
                except TypeError as e:
                    # 替身/老网关的 invoke 只接受 messages：退回两参数调用，不影响主流程
                    if 'config' not in str(e):
                        raise
                    resp = llm.invoke(lc_messages)
                content = getattr(resp, 'content', '') or ''
                model_name = getattr(getattr(resp, 'response_metadata', {}) or {}, 'model_name', '') \
                    if isinstance(getattr(resp, 'response_metadata', None), dict) else ''
                self._model_name = model_name or getattr(llm, 'model', '') or ''
                self._last_llm_meta = _extract_llm_meta(resp, label, self._model_name)
                self._usage_log.append(self._last_llm_meta)
                if self._last_llm_meta.get('prefix_sha256'):
                    self.logger.info(
                        '[策略研发] %s 前缀指纹 %s/%sB，输入 %s tokens（缓存命中 %s）',
                        label, self._last_llm_meta['prefix_sha256'],
                        self._last_llm_meta.get('prefix_bytes'),
                        self._last_llm_meta.get('input_tokens'),
                        self._last_llm_meta.get('cached_tokens'))
                return content
            except Exception as e:
                last_exc = e
                if self._is_rate_limit_error(e):
                    self.logger.warning(f'[策略研发] LLM 限流，退避 {self.backoff_seconds}s 重试: {e}')
                    continue
                raise
        raise last_exc or RuntimeError('LLM 调用失败')

    # ── prompt 构造 ──

    def _system_prompt(self) -> str:
        from backtest.json_compiler import export_llm_schema
        return (
            '你是 A 股量化策略 JSON 生成器。你只能输出一个符合下方 schema 约束的策略 JSON 对象。\n'
            '输出的最外层 JSON 必须形如 {"hypothesis": "本轮策略假设(一句话)", "strategy": {策略 JSON}}。\n'
            '策略 JSON 必须完全符合下方 schema；严禁使用「稳赚/必胜/躺赢/包赚/稳赢」等承诺收益的词汇。\n'
            '**成交时点（尾盘买/次日开盘卖…）由系统参数决定，不要写进 JSON，也不要为了表达'
            '「尾盘买入」去编造 MA20/RSI/MACD 之类的指标条件顶替**——那样做出来的策略与用户'
            '的想法完全不是一回事。\n\n'
            + export_llm_schema()
        )

    def _timing_note(self) -> str:
        """成交口径说明。口径对不上会让「尾盘买入、次日卖出」跑成别的东西。"""
        entry = self.entry_timing or 'next_open'
        exit_ = self.exit_timing or entry
        from backtest.strategy_base import TIMING_LABELS
        note = ('成交口径（已锁定，不可改）：'
                + TIMING_LABELS.get(entry, entry) + '买入 → '
                + TIMING_LABELS.get(exit_, exit_) + '卖出。\n')
        if entry == 'same_close' and exit_ in ('next_open', 'next_close'):
            note += ('引擎会按订单分别设置成交口径：买入按 T 日收盘，卖单按下一交易日'
                     + ('开盘' if exit_ == 'next_open' else '收盘')
                     + '成交。若卖点本意就是「尾盘买入、次日卖出」，'
                       '用 conditions.sell.hold_bars = 1 表达纯时序卖点，不要用指标条件。\n')
        else:
            note += '买点/卖点只写条件，不要写成交价——成交价由上面两行口径决定。\n'
        return note

    def _concept_note(self, idea: str) -> str:
        """把中文盘口概念翻成**引擎能精确表达**的价格条件（避免模型用 pct_change 顶替）。

        真机 2026-10-24：用户说「深V / 超跌反弹，第二天开盘买」，模型生成的是
        「当日涨幅≥5% 买入」——那是「大涨追高」，不是「日内下探后收回」。
        引擎其实有 low / high / pre_close / close / open 与 mul，足够精确表达，
        缺的只是把说法映射到条件；这里只在想法确实提到这些概念时追加提示。
        """
        text = str(idea or '').lower()
        if not any(k in text for k in _DIP_CONCEPT_WORDS):
            return ''
        return (
            '\n【条件表达提示】想法里的「深V / 超跌反弹 / 振幅」要用价格条件精确表达，'
            '**不要用 pct_change 代替**（pct_change>0 是「收涨」，不是「下探后收回」）：\n'
            '- 日内下探：low < {"func":"mul","args":["pre_close",0.95]}'
            '（最低价低于前收 5%；阈值按想法调整，如 0.93/0.97）\n'
            '- 收盘收回：close > open（或 close > {"func":"mul","args":["low",1.02]}）\n'
            '- 当日振幅大：high > {"func":"mul","args":["low",1.05]}\n'
            '- 以上可用 conditions.buy.logic=AND 组合；买点只写条件，成交时点由系统口径决定。\n'
        )

    def _initial_user_prompt(self, idea: str, universe: str) -> str:
        if self.pool_mode:
            target = (
                f'验证对象：混合池（{self.pool_desc or universe}；包含 10%/20% 涨跌停个股与 ETF）。\n'
                '⚠️ 这是池子验证，不是单票：**不要把某一只票的涨停上限写死**'
                '（不要用 <=9.9 / <=19.5 这种单票上限）；涨停买不进由 broker 按标的拦截。'
            )
            history = ''
        else:
            target = f'回测标的：{universe}（单标的日级回测）'
            history = self._history_note(universe)
        return (
            f'研究想法：{idea}\n'
            f'{target}\n'
            + self._timing_note() + '\n'
            '请设计一个与想法匹配的策略，输出 {"hypothesis": ..., "strategy": ...}。'
            'hypothesis 用一句话说明为什么该策略可能有效。\n'
            '**严格按研究想法实现**：想法里说的是「持有 N根后卖」就写 hold_bars=N，'
            '不要替换成自己觉得更好的指标条件。'
            + self._concept_note(idea)
            + self._prior_note()
            + history
        )

    def generate_for_pool(self, idea: str, pool_desc: str) -> GenResult:
        """池子优先：按混合池生成一份不写死单票涨停上限的策略。"""
        self.pool_mode = True
        self.pool_desc = str(pool_desc or '')
        self._reset_context(idea, 'POOL')
        return self._gen_with_retry(label=f'第{self._round_no}轮-池子生成')

    def _history_note(self, universe: str, max_runs: int = 3) -> str:
        """同标的的历史研究结论（跨 run 记忆）。

        背景：研究循环**内部**已有 append-only 历史（每轮看得到上一轮完整策略 JSON），
        但**新 run 从零开始**——同一标的会被反复拿去试上一次已被证伪的方向。
        这里只注入最近 3 次的**一行结论**，并明确标注「仅供参考、不要照抄」，
        避免把模型锚死在旧结论上。
        """
        try:
            from utils.cache.backtest_db import list_strategy_runs
            runs = [r for r in list_strategy_runs(limit=30)
                    if str(r.get('universe') or '') == str(universe)]
        except Exception:  # noqa: BLE001  历史读不到就不注入，绝不阻塞生成
            return ''
        cur = getattr(self, 'run_uuid', '') or ''
        runs = [r for r in runs if r.get('run_uuid') != cur][:max_runs]
        if not runs:
            return ''
        lines = ['', '【历史参考】同一标的最近的同类研究（**仅供参考，不要照抄**）：']
        for r in runs:
            best = r.get('best_oos_metrics') or {}
            sharpe = best.get('sharpe')
            sharpe_s = '—' if sharpe is None else f'{float(sharpe):.2f}'
            reason = str(r.get('stop_reason') or '').strip()
            hypo = str(r.get('best_hypothesis') or '').strip()
            lines.append(
                f"- {str(r.get('created_at') or '')[:10]}：{r.get('status') or '?'}"
                + (f"（{reason[:40]}）" if reason else '')
                + f"｜最优轮 OOS 夏普 {sharpe_s}"
                + (f"｜{hypo[:50]}" if hypo else '')
            )
        lines.append('若历史结论是「数据不足 / 无效」，先确认数据可用性，不要重复已被证伪的方向。')
        return '\n'.join(lines) + '\n'

    def _prior_note(self) -> str:
        """因子先验注入（S4 F5-A）。没先验文件 / 加载失败 → 空串，绝不编造。

        ⚠️ 先验文件缺失时**显式告警**（不再静默）：只要已有因子评估/跨池数据，
        注入空转就必须可见 —— 否则整条 F5-A 会悄无声息地不生效。
        """
        try:
            from backtest.factor_prior import (
                has_prior_source, load_prior, prior_prompt_text)
            prior = load_prior()
            if prior is None and has_prior_source():
                logger.warning(
                    '[策略研发] 未找到因子先验文件（output/factor_prior.json），但已存在'
                    '因子评估/跨池数据 —— 本轮不注入先验；请先跑 '
                    'scripts/ops/build_factor_prior.py 生成。')
            text = prior_prompt_text(prior)
        except Exception:  # noqa: BLE001
            return ''
        return ('\n' + text + '\n') if text else ''

    def _round_prompt(self, last: Optional[IterRecord], universe: str) -> str:
        """**只描述上一轮**（历史已在 messages 里，重复罗列既浪费 token 又打断缓存前缀）。"""
        lines = [f'回测标的：{universe}（单标的日级回测）', '', '上一轮结果：']
        if last is None:
            lines.append('- （没有上一轮结果，请按最初的想法给一个完整策略）')
        else:
            is_m = last.is_metrics or {}
            oos_m = last.oos_metrics or {}
            lines.append(f"- 第{last.round}轮 假设：{last.hypothesis or '（无）'}")
            lines.append(
                f"- IS: cagr={is_m.get('cagr')}, sharpe={is_m.get('sharpe')}, "
                f"trades={is_m.get('trades')}；数据：{_bars_text(is_m)}；"
                f"体检：{_health_label(is_m.get('health'))}")
            lines.append(
                f"- OOS: cagr={oos_m.get('cagr')}, sharpe={oos_m.get('sharpe')}, "
                f"mdd={oos_m.get('mdd')}, trades={oos_m.get('trades')}；"
                f"体检：{_health_label(oos_m.get('health'))}")
            lines.append(f"- 判定：{last.verdict}")
            if last.compile_error:
                # 失败轮必须把真实报错带进下一轮：否则模型不知道上一轮为什么没结果，
                # 很容易原样重抄（真机事故 2026-10-14：第 4 轮 RSI 周期 2 除零，
                # 第 5 轮又抄了一遍同类写法）。
                lines.append(f"- 失败原因：{str(last.compile_error)[:300]}")
            _ic = last.intent_check or {}
            if _ic.get('checked') and _ic.get('matches') is False:
                # 真机事故 2026-10-18：用户说「上涨买入、第二天卖出」，模型第 7 轮
                # 改成「下跌后首个反弹阳线买入」——核对早就记了 deviations，但没人用它，
                # 于是偏离一路继承。这里把偏离原文回灌，下一轮必须照原话实现。
                devs = [str(x) for x in (_ic.get('deviations') or [])][:4]
                lines.append(
                    '- ⚠️ 上一轮的实现**偏离了用户原话**：' + ('；'.join(devs) if devs else '（见核对结论）')
                    + '。下一轮必须把用户原话里的条件照原样实现（涨跌方向、买卖时点、阈值一个字都不能改），'
                      '只允许在用户没提的实现细节上做选择；**不要为了收益好看而保留偏离**。')
            if last.verdict == 'degenerate':
                # 真机事故 2026-10-18：模型连续 8 轮把「没数据/零成交」误判成「条件太严」，
                # 一路放宽到 MA3 仍在猜。必须把「空结果 ≠ 策略无效」说死。
                lines.append(
                    '- ⚠️ 上一轮是**空结果**（数据不足或零成交），不是条件太严：'
                    '不要在「条件太严/太松、周期长短、参数微调」上反复试错；'
                    '数据/体检不过关时，调整条件没有任何意义。')
        lines += [
            '',
            '请基于上一轮结果提出一个新变体（只改与上一轮失败原因相关的部分）：',
            '- 上一轮是**执行失败**（有失败原因）→ 改写法（如指标除零就调大周期或换指标），不要原样照抄；',
            '- 上一轮是**效果差**（OOS 回撤大/收益低）→ 改结构，而不是在同一结构上微调参数；',
            '- 标了「体检：数据缺口 / K线不足」的轮次，收益数字不可信，不要拿它当依据调参；',
            '- 只输出 {"hypothesis": "本轮假设", "strategy": {**完整**策略 JSON}}（不是 diff）。',
        ]
        # 因子先验只在首轮 user 里写一次（append-only 后它会一直留在上下文里），
        # 每轮重复会把 token 白烧掉。
        return '\n'.join(lines)

    # ── 上下文压缩（防无界增长）────────────────────────────────

    def _maybe_compress(self, history: List[IterRecord]) -> None:
        """超阈值时把「最近 2 轮之前」的对话压成一条摘要（只做一次）。

        ⚠️ system 必须留在 index 0；保留最近 2 轮原文（模型正在改的就是它）。
        压缩会**打断**前缀缓存，所以只在真正超限时触发，且每个生成器只压一次。
        """
        if self._compressed or len(self._messages) <= self.KEEP_TAIL_MESSAGES + 1:
            return
        rounds_done = sum(1 for role, _ in self._messages if role == 'assistant')
        total_chars = sum(len(content) for _, content in self._messages)
        if total_chars <= self.MAX_CONTEXT_CHARS and rounds_done <= self.MAX_CONTEXT_ROUNDS:
            return

        tail = self._messages[-self.KEEP_TAIL_MESSAGES:]
        dropped = self._messages[1:-self.KEEP_TAIL_MESSAGES]
        # 每轮 = (user, assistant) 两条；dropped 只会落在轮次边界上
        n_dropped = len(dropped) // 2
        rounds = list(history)[:n_dropped]
        if not rounds:
            return
        summary = ('【前 %d 轮摘要】（原文已压缩，结论如下；最近两轮保留原文）\n' % len(rounds)
                   + '\n'.join(_summary_line(it) for it in rounds))
        self._messages = [self._messages[0], ('user', summary)] + tail
        self._compressed = True
        self.logger.info('[策略研发] 上下文已压缩：%d 轮 → 摘要，保留最近 2 轮', len(rounds))


    # ── 生成入口（append-only）──────────────────────────────────

    def _reset_context(self, idea: str, universe: str) -> None:
        """首轮上下文：system 固定在 index 0，之后**只追加**。"""
        self._messages = [
            ('system', self._system_prompt()),
            ('user', self._initial_user_prompt(idea, universe)),
        ]
        self._round_no = 1
        self._compressed = False

    def restore_context(self, idea: str, universe: str,
                        iters: List[IterRecord]) -> None:
        """从 ``strategy_iter`` 重建 append-only 上下文（进程重启后**续跑**）。

        消息序列必须与正常 append-only **同形**，否则续跑第一轮的前缀就不再是
        「原 run 的前缀 + 追加」，prompt cache 直接归零：

            [system][user initial]
            + assistant(raw_1) [user(round_prompt(1))] assistant(raw_2) ... assistant(raw_N)

        ``raw_k`` 由落库的 hypothesis + strategy 还原成模型原始输出的外层形状
        （``{"hypothesis":..., "strategy":...}``），并按 ``ASSISTANT_RAW_LIMIT`` 截断。
        某轮没有 strategy_json / JSON 解析失败（生成失败轮）时整条跳过并 warning：
        续跑宁可用一条更短的历史，也不能因为一条脏数据起不来。

        注意：首轮 user 用 run.idea 原文重建（preflight 的「数据说明」只进过首次
        发送的 prompt，库里没存，不在这里补——补了反而只多出一段不一致）。
        """
        self._messages = [
            ('system', self._system_prompt()),
            ('user', self._initial_user_prompt(idea, universe)),
        ]
        self._round_no = len(iters)
        self._compressed = False

        valid: List[Tuple[IterRecord, str]] = []
        for it in iters:
            raw = self._restore_assistant_raw(it)
            if raw is not None:
                valid.append((it, raw))
        for idx, (it, raw) in enumerate(valid):
            self._messages.append(('assistant', raw))
            if idx < len(valid) - 1:
                # 与 mutate() 完全一致：下一轮的「上一轮结果」prompt
                self._messages.append(('user', self._round_prompt(it, universe)))
        self.logger.info('[策略研发] 续跑上下文已重建：%d 轮记录 → %d 条消息',
                         len(iters), len(self._messages))

    def _restore_assistant_raw(self, it: IterRecord) -> Optional[str]:
        """一条 IterRecord → 模型原文（外层 wrapper 形状）；不可还原返回 None。"""
        if not it.strategy_json:
            self.logger.warning(
                '[策略研发] 续跑重建跳过第 %s 轮：没有 strategy_json（生成失败轮）',
                it.round)
            return None
        try:
            strategy = json.loads(it.strategy_json)
        except (json.JSONDecodeError, TypeError) as e:
            self.logger.warning(
                '[策略研发] 续跑重建跳过第 %s 轮：strategy_json 解析失败（%s）',
                it.round, e)
            return None
        raw = json.dumps({'hypothesis': it.hypothesis or '', 'strategy': strategy},
                         ensure_ascii=False)
        return raw[:self.ASSISTANT_RAW_LIMIT]

    def generate_initial(self, idea: str, universe: str) -> GenResult:
        """想法 → 策略 JSON + 假设（建立 append-only 上下文的第 1 轮）。"""
        self._reset_context(idea, universe)
        return self._gen_with_retry(label=f'第{self._round_no}轮-生成')

    def mutate(self, idea: str, universe: str, history: List[IterRecord]) -> GenResult:
        """追加「上一轮结果 + assistant 原文」后变异（历史不再重复罗列）。"""
        if not self._messages:
            # 容错：没走 generate_initial 就直接 mutate（单测/重入）
            self._reset_context(idea, universe)
        self._round_no += 1
        last = history[-1] if history else None
        self._messages.append(('user', self._round_prompt(last, universe)))
        self._maybe_compress(history)
        return self._gen_with_retry(label=f'第{self._round_no}轮-生成')

    def _gen_with_retry(self, max_retries: Optional[int] = None,
                        label: str = '') -> GenResult:
        """生成 + 编译校验 + 错误反馈重试（≤ max_retries 次）。

        非法 JSON / 编译失败 → 把错误信息（含 compiler ValueError 的支持清单）
        作为反馈消息重试；连续失败返回 ok=False + 可读原因（不暴露堆栈）。

        ⚠️ 反馈只追加在**本次调用**的末尾（base + feedback），不改动 self._messages 的
        前缀；只有成功的那次 raw 才作为 assistant 落进跨轮上下文 —— 这样下一轮的前缀
        恰好是「上一轮请求前缀 + 本次问答」，provider 才能命中缓存。
        """
        retries = self.max_retries if max_retries is None else max_retries
        base = list(self._messages)
        feedback: List[Tuple[str, str]] = []
        last_error = ''
        attempts = 0
        model_name = ''

        for attempt in range(retries + 1):
            attempts += 1
            call_label = label + (f'-重试{attempt}' if attempt else '')
            try:
                raw = self._invoke_llm(base + feedback, label=call_label)
            except Exception as e:
                last_error = f'LLM 调用失败：{type(e).__name__}'
                self.logger.warning(f'[策略研发] LLM 调用异常（第 {attempt} 次尝试）: {e}')
                # 网关已做重试与降级；这里不再无限重试
                break
            model_name = getattr(self, '_model_name', '') or ''

            obj = extract_json_block(raw)
            if obj is None:
                last_error = f'第 {attempt} 次输出无法解析为 JSON'
                feedback.append(('assistant', raw[:2000]))
                feedback.append(('user',
                                 '你的输出不是合法 JSON。请只输出一个 JSON 对象：'
                                 '{"hypothesis": "...", "strategy": {...}}，不要任何解释文字。'))
                self.logger.warning(f'[策略研发] {last_error}')
                continue

            hypothesis, strategy_obj = _split_gen_output(obj)
            strategy_obj = sanitize_strategy(strategy_obj)
            hypothesis = sanitize_text(hypothesis)
            strategy_str = json.dumps(strategy_obj, ensure_ascii=False)

            # 校验双门（错误信息自带支持清单，直接回灌给模型）：
            # 1) 保存闸门 validate_strategy_json —— 与 POST /strategies 同一套规则，
            #    能在回测之前拦住「畸形操作数 / 周期 0 / 非法 field」这类会被静默改写的写法；
            # 2) 编译 —— 唯一能发现结构性问题的一步。
            try:
                from backtest.json_compiler import compile_strategy, validate_strategy_json
                problems = validate_strategy_json(strategy_str)
                if problems:
                    raise ValueError('；'.join(problems[:5]))
                compile_strategy(strategy_str)
            except Exception as e:
                last_error = str(e) or type(e).__name__
                feedback.append(('assistant', raw[:2000]))
                feedback.append(('user',
                                 f'策略 JSON 编译失败：{last_error}\n'
                                 '请根据错误信息修正后，重新只输出 '
                                 '{"hypothesis": ..., "strategy": {...}}。'))
                self.logger.warning(f'[策略研发] 第 {attempt} 次编译失败: {last_error[:200]}')
                continue

            # 只有成功输出才进跨轮上下文（重试反馈仅本次可见）——
            # 下一轮的前缀 = 本次前缀 + 本次问答，缓存才能续上。
            self._messages.append(
                ('assistant', raw[:self.ASSISTANT_RAW_LIMIT]))
            return GenResult(ok=True, strategy_json=strategy_str,
                             hypothesis=hypothesis or '（未提供假设）',
                             model=model_name, attempts=attempts)

        # 失败轮也把「模型最后说了什么」留在上下文里，否则下一轮会出现连续两条 user
        if feedback:
            last_assistant = next((c for r, c in reversed(feedback) if r == 'assistant'), '')
            if last_assistant:
                self._messages.append(
                    ('assistant', last_assistant[:self.ASSISTANT_RAW_LIMIT]))
        return GenResult(ok=False, strategy_json=None,
                         hypothesis='', error=last_error or '未知生成错误',
                         model=model_name, attempts=attempts)


def _to_langchain_messages(messages: List[Tuple[str, str]]):
    """把 (role, content) 元组列表转成 LangChain 消息对象。"""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    mapping = {'system': SystemMessage, 'user': HumanMessage, 'assistant': AIMessage}
    return [mapping.get(role, HumanMessage)(content) for role, content in messages]


def _split_gen_output(obj: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """解析生成输出：优先 {"hypothesis", "strategy"} 结构；兜底整体即策略 JSON。"""
    if isinstance(obj.get('strategy'), dict):
        return str(obj.get('hypothesis', '')), obj['strategy']
    return '', obj


# ============================================================
# IS/OOS 双段回测执行器
# ============================================================


class IsoOsRunner:
    """IS/OOS 双段回测（数据截断两次回测，SP-2 定论）。

    指标摘要五件套：{cagr, sharpe, mdd, win_rate, trades}。
    结果按 (strategy_hash, universe, period) 进程内缓存（变异微调常重复命中）。
    """

    def __init__(self, split_ratio: float = 0.7, initial_cash: float = 1000000,
                 engine: Any = None, benchmark: str = '', run_uuid: Optional[str] = None,
                 entry_timing: str = 'next_open', exit_timing: str = '',
                 origin: str = 'research', ref_id: str = ''):
        self.split_ratio = split_ratio
        self.initial_cash = initial_cash
        self.entry_timing = entry_timing
        # 卖出口径：留空 = 跟随买入口径（保持「买卖同口径」这个最常见默认）
        self.exit_timing = exit_timing or entry_timing
        self._engine = engine          # 惰性取 BacktestEngine（可注入替身）
        self.benchmark = benchmark
        self.run_uuid = run_uuid       # 研发任务号：写入账本 ref_id
        self.origin = origin           # 账本来源：研发 research / 评判 eval（Plan 3 任务 3）
        self.ref_id = ref_id           # 触发它的记录（评判场景 = 被评判的 run_id）；留空回退 run_uuid
        self._cache: Dict[Tuple[str, str, str], Tuple[dict, dict]] = {}

    # ── 引擎句柄 ──

    def _get_engine(self):
        if self._engine is None:
            from backtest.engine import BacktestEngine
            self._engine = BacktestEngine()
        return self._engine

    # ── 交易日切分 ──

    def _trading_days(self, universe: str, start: str, end: str) -> List[str]:
        """标的在区间内的实际交易日序列（本地库 stock_daily，不外呼）。"""
        from utils.cache.market_data_db import get_stock_daily
        df = get_stock_daily(universe, start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        return [str(d)[:10] for d in df['trade_date'].tolist()]

    def split_period(self, start: str, end: str, universe: str,
                     ratio: Optional[float] = None) -> Tuple[Tuple[str, str], Tuple[str, str]]:
        """按交易日边界切分。

        OOS 起点取切分日**次一交易日**（Spike 修订 5，避免 1 天重复计价）。
        无本地数据时退化为自然日比例切分（近似）。
        """
        r = ratio if ratio is not None else self.split_ratio
        days = self._trading_days(universe, start, end)
        if len(days) < 10:
            # 无数据兜底：自然日近似（生产路径不应走到；单测不依赖）
            from datetime import datetime, timedelta
            d0 = datetime.strptime(start, '%Y-%m-%d')
            d1 = datetime.strptime(end, '%Y-%m-%d')
            mid = d0 + timedelta(days=int((d1 - d0).days * r))
            return (start, mid.strftime('%Y-%m-%d')), (mid.strftime('%Y-%m-%d'), end)
        split_idx = int(len(days) * r)          # IS 为前 r（0..split_idx-1）
        split_idx = max(1, min(split_idx, len(days) - 1))
        is_end = days[split_idx - 1]
        oos_start = days[split_idx]             # 切分日次一交易日
        return (start, is_end), (oos_start, end)

    # ── 双段回测 ──

    def run_both(self, strategy_json: str, universe: str,
                 period: Tuple[str, str]) -> Tuple[dict, dict]:
        """编译 → engine.run(IS) / engine.run(OOS) → 指标摘要五件套。"""
        from backtest.json_compiler import compile_strategy
        from backtest.strategies import _STRATEGY_REGISTRY

        s_hash = strategy_hash(strategy_json)
        cache_key = (s_hash, universe, f'{period[0]}~{period[1]}')
        if cache_key in self._cache:
            return self._cache[cache_key]

        strategy_cls = compile_strategy(strategy_json)
        strategy_id = f'sr_{s_hash}'
        _STRATEGY_REGISTRY[strategy_id] = strategy_cls

        (is_start, is_end), (oos_start, oos_end) = self.split_period(
            period[0], period[1], universe)

        is_metrics = self._run_segment(strategy_id, universe, is_start, is_end, 'IS')
        oos_metrics = self._run_segment(strategy_id, universe, oos_start, oos_end, 'OOS')

        is_metrics = clamp_win_rate(is_metrics)
        oos_metrics = clamp_win_rate(oos_metrics)
        self._cache[cache_key] = (is_metrics, oos_metrics)
        return is_metrics, oos_metrics

    def preflight(self, universe: str, period: Tuple[str, str]) -> Dict[str, Any]:
        """研发前的数据闸门（由 StrategyResearchLoop 在调 LLM 之前调用）。

        只读本地库，不外呼。不达标抛 ValueError —— 文案含「只有 N 根 K 线/非日线」，
        直接进 strategy_run.stop_reason，用户与模型都看得见。
        """
        return preflight_data(universe, period[0], period[1])

    def _run_segment(self, strategy_id: str, universe: str,
                     start: str, end: str, label: str) -> dict:
        """单段回测并提取指标；失败抛出（由状态机决定任务失败）。"""
        config = {
            'code': universe,
            'strategy_id': strategy_id,
            'strategy_name': strategy_id,
            'start_date': start,
            'end_date': end,
            'initial_cash': self.initial_cash,
            'params': {},
            'adjust_type': 'qfq',
            'entry_timing': self.entry_timing,
            'exit_timing': self.exit_timing,
            # 资产类型必须由研究执行器显式透传：engine 默认 'stock'，ETF 会被当成
            # 股票收印花税、并挂上个股涨跌停口径。识别规则与批量回测同一份。
            'asset_type': _asset_type_of(universe),
        }
        # ⚠️ 空 benchmark 必须**不传**，不能传 ''：engine 用
        # `config.get('benchmark', '000300')`，显式传空串会跳过默认值 → 覆盖率分母退回
        # 「工作日估算」，A 股节假日被当成缺口。实测 600519 的 8 个月 IS 段：
        # 170/180 = 94.4% < 95% → 每一轮都被判 data_gap/degenerate，
        # 与真实数据质量无关（2026-10-19 真实 2 轮跑出来）。不传则用 000300 真实交易日。
        if self.benchmark:
            config['benchmark'] = self.benchmark
        logger.info(f'[策略研发] {label} 回测: {universe} {start}~{end}')
        # ⚠️ origin / ref_id 是 BacktestEngine.run 的关键字参数（backtest/engine.py:35-37），
        # 写进 config 无效（引擎只看 kwargs）——故身份在此透传，不进上面的 config 字典。
        result = self._get_engine().run(
            dict(config),
            run_id=f'SR-{label}-{strategy_id}',
            origin=self.origin,
            ref_id=self.ref_id or self.run_uuid or '',
        )
        # ⚠️ 两个收益口径必须都给：
        #   cagr（annual_return）= **年化**，回答「平均每年赚多少」；
        #   total_return        = **区间总收益**，回答「这段回测整体赚了多少」。
        # 早期只映射了 cagr，结果卡/研究面板上用户只能看到年化，看不到区间总收益。
        #
        # 体检字段（health/data_bars/coverage_pct）一并带回：研发循环过去只拿 6 个
        # 数字，把「K 线 16 根、体检 insufficient_bars」整条信息丢掉了，模型只能靠
        # 猜（真机事故 2026-10-18）。塞进 metrics JSON 而不是新增表列，免迁移。
        return {
            'cagr': result.get('annual_return'),
            # 年化口径标记：老 run 是 rnorm100（252/bar 数），新 run 是自然日 CAGR。
            # 卡片据此决定「能不能展示年化」——两种口径的数字不可混着比。
            'annual_method': 'calendar_days',
            'total_return': result.get('total_return'),
            'sharpe': result.get('sharpe_ratio'),
            'mdd': result.get('max_drawdown'),
            'win_rate': result.get('win_rate'),
            'trades': result.get('trade_count'),
            'health': result.get('health') or '',
            'health_detail': result.get('health_detail') or '',
            'data_bars': result.get('data_bars'),
            'expected_bars': result.get('expected_bars'),
            'coverage_pct': result.get('coverage_pct'),
            # 中性带信息：胜率为 None 时必须能解释「为什么算不出来」——
            # 否则报告只能写「胜率不可用」，用户无从判断是数据问题还是策略问题。
            'neutral_count': result.get('neutral_count'),
            'neutral_rate': result.get('neutral_rate'),
            'effective_trades': result.get('effective_trades'),
        }


class MultiWindowRunner:
    """多窗口稳健性评估（P1 设计 §3）：K 段连续 purged 窗口 + 段间 embargo。

    不做 ML 的 train/test（本项目无训练环节）——本类是「同一策略在 K 个独立窗口上
    的表现分布」的生产者，回答「稳定还是碰巧」，并给切分敏感性一个落点。

    - K 段等长（余数并入末段）、段间留 ``embargo_bars`` 个交易日 **gap**（不属于
      任何窗口——防跨窗持仓 / 相邻 bar 信息泄漏）
    - 每窗独立 ``engine.run``（空仓开始）——边界冷启动 + embargo 双保险
    - 数据不足（有效交易日 < 10×K）→ 退化为单窗（全区间），由调用方感知
    """

    def __init__(self, n_windows: Optional[int] = None,
                 embargo_bars: Optional[int] = None,
                 initial_cash: float = 1000000, engine: Any = None,
                 run_uuid: Optional[str] = None, entry_timing: str = 'next_open',
                 exit_timing: str = ''):
        from config import Config
        if n_windows is None:
            n_windows = int(getattr(Config, 'BACKTEST_MULTI_WINDOW_N', 4) or 4)
        if embargo_bars is None:
            embargo_bars = int(getattr(Config, 'BACKTEST_MULTI_WINDOW_EMBARGO', 5) or 5)
        self.n_windows = max(1, int(n_windows))
        self.embargo_bars = max(0, int(embargo_bars))
        # 复用 IsoOsRunner 的单段执行 / 交易日获取（组合而非继承：语义不同）
        self._runner = IsoOsRunner(initial_cash=initial_cash, engine=engine,
                                   run_uuid=run_uuid, entry_timing=entry_timing,
                                   exit_timing=exit_timing)

    # ── 切分 ──

    def split_windows(self, start: str, end: str,
                      universe: str) -> List[Tuple[str, str]]:
        """切 K 段连续窗口 + 段间 embargo gap。返回 ``[(seg_start, seg_end), ...]``。

        数据不足 / 段长过短 → 退化为 ``[(start, end)]``（单窗 = 全区间）。
        """
        days = self._runner._trading_days(universe, start, end)
        k, g = self.n_windows, self.embargo_bars
        if len(days) < max(10, 10 * k):
            return [(start, end)]
        seg = (len(days) - g * (k - 1)) // k
        if seg < 5:
            return [(start, end)]
        windows: List[Tuple[str, str]] = []
        pos = 0
        for i in range(k):
            chunk = days[pos:pos + seg]
            windows.append((chunk[0], chunk[-1]))
            pos += seg
            if i < k - 1:
                pos += g        # embargo gap：跳过，不属于任何窗口
        tail = days[pos:]
        if tail:
            windows[-1] = (windows[-1][0], tail[-1])   # 余数并入末段
        return windows

    # ── 执行与摘要 ──

    def run_windows(self, strategy_json: str, universe: str,
                    period: Tuple[str, str]) -> Dict[str, Any]:
        """逐窗回测 → 每窗五件套 + 分布摘要（P1 设计 §3.2 返回结构）。"""
        import statistics as _st
        from backtest.json_compiler import compile_strategy
        from backtest.strategies import _STRATEGY_REGISTRY

        s_hash = strategy_hash(strategy_json)
        strategy_cls = compile_strategy(strategy_json)
        strategy_id = f'sr_{s_hash}'
        _STRATEGY_REGISTRY[strategy_id] = strategy_cls

        windows = self.split_windows(period[0], period[1], universe)
        results: List[Dict[str, Any]] = []
        for i, (s, e) in enumerate(windows, 1):
            metrics = self._runner._run_segment(strategy_id, universe, s, e, f'MW{i}')
            results.append({'label': f'W{i}', 'start': s, 'end': e, **metrics})

        sharpes = [r['sharpe'] for r in results if r.get('sharpe') is not None]
        summary = {
            'n_windows': len(results),
            'embargo_bars': self.embargo_bars,
            'sharpe_min': round(min(sharpes), 4) if sharpes else None,
            'sharpe_median': round(_st.median(sharpes), 4) if sharpes else None,
            'sharpe_max': round(max(sharpes), 4) if sharpes else None,
            'positive_windows': sum(1 for r in results if (r.get('total_return') or 0) > 0),
            'trade_windows': sum(1 for r in results if (r.get('trades') or 0) > 0),
        }
        return {'strategy_hash': s_hash, 'universe': universe,
                'period': list(period), 'windows': results, 'summary': summary}


# ============================================================
# 落库适配器（包装 utils.cache.backtest_db 的 CRUD，可注入替身）
# ============================================================


class BacktestRunStore:
    """StrategyResearchLoop 的 strategy_run / strategy_iter 读写适配。"""

    def create_run(self, run_uuid: str, idea: str, universe: str, period: str,
                   source: str = 'panel', dialog_uuid: Optional[str] = None) -> None:
        from utils.cache.backtest_db import create_strategy_run
        from backtest.engine import ENGINE_VERSION
        create_strategy_run(run_uuid, idea, universe, period,
                            source=source, dialog_uuid=dialog_uuid,
                            engine_version=ENGINE_VERSION)

    def update_run(self, run_uuid: str, **kwargs) -> None:
        from utils.cache.backtest_db import update_strategy_run
        update_strategy_run(run_uuid, **kwargs)

    def get_run(self, run_uuid: str) -> Optional[Dict[str, Any]]:
        from utils.cache.backtest_db import get_strategy_run
        return get_strategy_run(run_uuid)

    def insert_iter(self, run_uuid: str, record: IterRecord) -> int:
        from utils.cache.backtest_db import insert_strategy_iter
        return insert_strategy_iter(
            run_uuid, round_no=record.round, hypothesis=record.hypothesis,
            strategy_json=record.strategy_json, strategy_hash=record.strategy_hash,
            strategy_diff=record.strategy_diff, compile_ok=record.compile_ok,
            compile_error=record.compile_error,
            is_metrics=record.is_metrics, oos_metrics=record.oos_metrics,
            overfit_flag=record.overfit_flag, verdict=record.verdict,
            model=record.model, intent_check=record.intent_check,
        )

    def set_iter_verdict(self, iter_id: int, verdict: str) -> None:
        from utils.cache.backtest_db import update_strategy_iter
        update_strategy_iter(iter_id, verdict=verdict)


# ============================================================
# 启动收敛：残留 running → interrupted（可续跑）
# ============================================================


def mark_interrupted_runs(reason: str = '服务重启，任务中断') -> int:
    """把进程重启后残留的 ``running`` 策略研究任务收敛成 ``interrupted``。

    为什么要单独一个状态：研发循环的 append-only 上下文只存在内存里，进程一挂就没了，
    run 会永久停在 ``running``（前端一直转圈、SSE 一直等）。半成品轮次已经落在
    ``strategy_iter`` 里，所以正确的收尾不是 failed，而是「可恢复的中间态」——
    用户点续跑就能从第 N+1 轮接着跑同一个 run。

    ⚠️ 必须在 backtest 表结构就绪之后调用（启动时序见 ``server/app.py::lifespan``）。
    不写 ``completed_at``：interrupted 不是终态，续跑成功后才会有完成时间。
    """
    from utils.cache.backtest_db import get_backtest_db
    with get_backtest_db() as conn:
        count = conn.execute(
            "UPDATE strategy_run SET status='interrupted', stop_reason=? "
            "WHERE status='running'", (reason,)
        ).rowcount
    if count:
        logger.warning('[策略研发] 启动收敛：%d 个 running 任务已标记为 interrupted（可续跑）',
                       count)
    return int(count or 0)


# ============================================================
# 循环状态机
# ============================================================


class StrategyResearchLoop:
    """循环状态机（GEN→COMPILE→BACKTEST→EVAL→DECIDE，终止→REPORT）。"""

    MAX_ROUNDS = MAX_ROUNDS
    OVERFIT_RATIO = OVERFIT_RATIO

    def __init__(self, run_uuid: str, db: Any, generator: Any, runner: Any,
                 progress_cb: Optional[Callable[[str, dict], None]] = None,
                 budget: Optional[int] = None,
                 cancel_check: Optional[Callable[[], bool]] = None):
        """
        Args:
            run_uuid: 任务标识
            db: 落库适配（BacktestRunStore 或同接口替身）
            generator: 策略生成器（StrategyGenerator 或替身）
            runner: IS/OOS 执行器（IsoOsRunner 或替身）
            progress_cb: 进度回调 (event_type, payload)，SSE 推送
            budget: LLM 调用次数预算（None = 不限）
            cancel_check: 取消哨点（返回 True 时中断）
        """
        self.run_uuid = run_uuid
        self.db = db
        self.generator = generator
        self.runner = runner
        self._raw_progress_cb = progress_cb or (lambda _e, _d: None)
        # C 批：顺这条已有出口做 trace 埋点，不侵入循环逻辑。
        # 「每轮 LLM 调用 + IS/OOS 双段回测」是策略研发最贵也最不可见的一段，
        # 之前完全不进trace（run 由 LangGraph 建，策略压根不进图），
        # 月历/token 统计里凭空消失。埋点失败一律吞掉，绝不影响研发主流程。
        self.progress_cb = self._wrap_progress(self._raw_progress_cb)
        self.budget = budget
        self.cancel_check = cancel_check or (lambda: False)
        self._llm_calls = 0
        # 数据闸门给的「实际区间短于请求区间」说明：只进首轮 prompt，不改 idea 落库
        self._preflight_note = ''

    # ── trace 埋点（C 批）──

    #: progress 事件 → (轮号, phase)
    _TRACE_PHASES = {
        'strategy_iter_start': 'gen',
        'strategy_iter_done': 'decide',
        'strategy_run_done': 'terminate',
    }

    def _wrap_progress(self, inner: Callable[[str, dict], None]):
        """把 progress 回调包一层：转发 + 写 trace step。

        刻意**不**改 run/step 主体结构：这条回调是「状态机唯一对外出口」，
        在这里加一层转发就能拿到全部迭代事实，且单测里替换progress_cb 的替身
        仍能工作（包装发生在 __init__，替身传进来时同样会被包）。
        """
        def wrapped(event: str, payload: dict) -> None:
            try:
                self._trace_event(event, payload)
            except Exception:
                logger.debug('[策略研发] trace 埋点失败（忽略）', exc_info=True)
            try:
                inner(event, payload)
            except Exception:
                logger.warning('[策略研发] progress 回调异常（忽略）', exc_info=True)
        return wrapped

    def _trace_event(self, event: str, payload: dict) -> None:
        from agents.shared.strategy_trace import current_iter_hook
        hook = current_iter_hook()
        if hook is None:
            return
        phase = self._TRACE_PHASES.get(event)
        if phase is None:
            return
        data = dict(payload or {})
        if event == 'strategy_run_done':
            # 终止事件没有轮号，用 0 表示「整体」，别混进某一轮
            hook(0, phase, {'event': event, 'status': data.get('status'),
                            'stop_reason': data.get('stop_reason'),
                            'total_iters': data.get('total_iters')})
            return
        hook(
            int(data.get('round') or 0),
            phase,
            {'event': event,
             'hypothesis': str(data.get('hypothesis') or '')[:120],
             'verdict': data.get('verdict')},
        )

    # ── 主循环 ──

    def _abort_for_data(self, message: str) -> RunResult:
        """数据不足：整个任务直接 failed，一轮也不跑。

        和「回测执行失败」不同：这不是某一轮策略写法的问题，换条件/换指标都救不回来，
        继续迭代只会烧预算并给出一串不可信的数字（2026-10-18 事故：8 轮全跑在月线上）。
        """
        stop_reason = f'数据不足：{message}'
        try:
            self.db.update_run(self.run_uuid, status='failed', stop_reason=stop_reason,
                               best_iter_id=None, total_iters=0,
                               completed_at=time.strftime('%Y-%m-%d %H:%M:%S'))
        except Exception:  # noqa: BLE001 - 落库失败也要把结果返回给调用方
            logger.warning('[策略研发] 数据不足状态落库失败', exc_info=True)
        logger.warning('[策略研发] %s 数据不足，已终止：%s', self.run_uuid, message)
        self.progress_cb('strategy_run_done', {
            'run_uuid': self.run_uuid, 'status': 'failed',
            'stop_reason': stop_reason, 'total_iters': 0,
            'best_iter_id': None, 'best': None,
        })
        return RunResult(run_uuid=self.run_uuid, status='failed',
                         stop_reason=stop_reason, total_iters=0, best_iter=None)

    def run(self, resume_iters: Optional[List[IterRecord]] = None) -> RunResult:
        """主循环：逐轮执行直至终止条件命中；结束后选优落库。

        Args:
            resume_iters: 断点续跑时从 ``strategy_iter`` 读回的已完成轮次。
                非空时**不建新 run、不重跑首轮**：用
                ``StrategyGenerator.restore_context`` 把历史还原成 append-only 上下文，
                从第 ``len(resume_iters) + 1`` 轮继续（同一个 run_uuid）。
                **预算按本次续跑重新计**（``_llm_calls`` 从 0 起）：一次续跑最多再烧
                ``budget`` 次 LLM 调用，不与中断前已消耗的次数合并。
        """
        history: List[IterRecord] = list(resume_iters or [])
        status = 'done'
        stop_reason = STOP_MAX_ROUNDS
        backtest_failures = 0          # 连续回测执行失败计数（见 MAX_BACKTEST_FAILURES）
        start_round = 1

        # ── 数据前置闸门：在第一次 LLM 调用之前 ──
        # 不达标直接结束（不烧 8 轮预算）。只有真实 runner（IsoOsRunner）才有这个能力，
        # 单测里的替身没有 preflight 就跳过 —— 否则单测会去读真实行情库。
        preflight = getattr(self.runner, 'preflight', None)
        if callable(preflight):
            universe, (p_start, p_end) = self._universe(), self._period()
            try:
                info = preflight(universe, (p_start, p_end)) or {}
            except ValueError as e:
                return self._abort_for_data(str(e))
            # 实际区间短于请求区间 → 写进首轮 prompt，不静默缩短
            self._preflight_note = short_span_note(info, p_start, p_end)
            if self._preflight_note:
                logger.warning('[策略研发] %s %s', self.run_uuid, self._preflight_note)

        if resume_iters:
            start_round = len(resume_iters) + 1
            # 首轮 prompt 早就发过了：这里只还原上下文，不再 generate_initial
            self.generator.restore_context(self._idea(), self._universe(), history)
            logger.info('[策略研发] %s 断点续跑：已完成 %d 轮，从第 %d 轮继续',
                        self.run_uuid, len(resume_iters), start_round)
            if start_round > MAX_ROUNDS:
                logger.warning('[策略研发] %s 已完成 %d 轮（≥ MAX_ROUNDS=%d），本次续跑无新轮次',
                               self.run_uuid, len(resume_iters), MAX_ROUNDS)

        for round_no in range(start_round, MAX_ROUNDS + 1):
            if self.cancel_check():
                status, stop_reason = 'cancelled', STOP_CANCELLED
                break
            if self._budget_exhausted():
                status, stop_reason = 'done', STOP_BUDGET
                break

            record = self.step(round_no, history)
            history.append(record)

            if record.verdict == 'failed':
                if record.failure_kind != 'backtest':
                    # 生成侧连续非法（PRD FR-02 / AC-3）：终止，任务判 failed
                    status, stop_reason = 'failed', STOP_FAILED
                    break
                # 回测**执行失败**（引擎/数据/指标异常）：把真实报错回灌给模型
                # （见 _round_prompt 的「失败原因」行），换一轮重试；
                # 连续 MAX_BACKTEST_FAILURES 轮都失败才终止整次研发。
                backtest_failures += 1
                if backtest_failures >= MAX_BACKTEST_FAILURES:
                    status, stop_reason = 'failed', STOP_FAILED
                    break
                logger.warning('[策略研发] 第 %d 轮回测执行失败（连续 %d/%d），换一轮重试：%s',
                               round_no, backtest_failures, MAX_BACKTEST_FAILURES,
                               record.compile_error)
                continue

            backtest_failures = 0
            stop = self.should_stop(history)
            if stop:
                status = 'done'
                stop_reason = stop
                break

        best = self.select_best(history) if status == 'done' else None
        # 最后一轮是回测失败、又没选出可用策略时如实报 failed：
        # 否则会出现「任务显示完成、却什么都没有」的假绿。
        if status == 'done' and best is None and history and history[-1].verdict == 'failed':
            status, stop_reason = 'failed', STOP_FAILED
        if best is not None and best.id is not None:
            try:
                self.db.set_iter_verdict(best.id, 'best')
            except Exception:
                logger.warning('[策略研发] best verdict 回写失败', exc_info=True)

        total_iters = len(history)
        self.db.update_run(self.run_uuid, status=status, stop_reason=stop_reason,
                           best_iter_id=(best.id if best and best.id else None),
                           total_iters=total_iters,
                           completed_at=time.strftime('%Y-%m-%d %H:%M:%S'))
        self.progress_cb('strategy_run_done', {
            'run_uuid': self.run_uuid, 'status': status, 'stop_reason': stop_reason,
            'best_iter_id': (best.id if best and best.id else None),
            'total_iters': total_iters,
            'best': _record_payload(best) if best else None,
        })
        return RunResult(run_uuid=self.run_uuid, status=status,
                         stop_reason=stop_reason, total_iters=total_iters,
                         best_iter=best)

    # ── 单轮（可单测） ──

    def step(self, round_no: int, history: List[IterRecord]) -> IterRecord:
        """执行一轮：GEN(+重试) → BACKTEST → EVAL → 落库。"""
        self._llm_calls += 1
        if round_no == 1:
            idea = self._idea()
            if self._preflight_note:
                # 数据说明只进 prompt，不改 run.idea（库里仍是用户原话）
                idea = f'{idea}\n\n【数据说明】{self._preflight_note}'
            gen = self.generator.generate_initial(idea, self._universe())
        else:
            gen = self.generator.mutate(self._idea(), self._universe(), history)

        if not gen.ok:
            # 连续 3 次非法 → 轮次 failed（整任务终止，PRD FR-02 / AC-3）
            record = IterRecord(
                round=round_no, hypothesis='', strategy_json=None,
                compile_ok=0, compile_error=gen.error, verdict='failed',
                failure_kind='gen',
                model=gen.model,
            )
            self._persist(record)
            self.progress_cb('strategy_iter_done', {
                'run_uuid': self.run_uuid, 'round': round_no,
                'hypothesis': '', 'error': gen.error, 'verdict': 'failed',
            })
            return record

        record = IterRecord(
            round=round_no, hypothesis=gen.hypothesis,
            strategy_json=gen.strategy_json, strategy_hash=strategy_hash(gen.strategy_json or ''),
            compile_ok=1, model=gen.model,
        )
        prev_json = history[-1].strategy_json if history else None
        record.strategy_diff = diff_strategy(prev_json, gen.strategy_json)

        # 意图保真核对：**生成即核对**（在回测之前）——「是不是用户说的那个策略」与
        # 「赚不赚钱」是两件事，不能等跑完 8 轮才发现条件被改写（实测 2026-10-14：
        # 用户说「20 日新高 + 1.5 倍量」，8 轮全用布林上轨/高点均线且丢了倍数门槛）。
        # fail-open：核对不了（开关关/LLM 挂）记 checked=False，绝不拦研发。
        # 能力缺口：本轮模型**想要**但引擎没有的东西（如「20 日新高」「1.5 倍」当年都没有）。
        # 先单独编译一次只为收集缺口（编译失败也照样收集，随后 run_both 会如实报错）。
        round_gaps: List[Dict[str, Any]] = []
        try:
            from backtest.json_compiler import compile_strategy as _compile_probe
            _compile_probe(gen.strategy_json or '{}', gap_sink=round_gaps)
        except Exception:
            pass
        try:
            from backtest.intent_fidelity import check_intent
            slots = self._slots() if hasattr(self, '_slots') else {}
            record.intent_check = check_intent(
                idea=self._idea(),
                entry_text=str((slots.get('entry') or {}).get('value') or '') if isinstance(slots.get('entry'), dict) else '',
                exit_text=str((slots.get('exit') or {}).get('value') or '') if isinstance(slots.get('exit'), dict) else '',
                strategy_json=gen.strategy_json,
                logger=logger,
            )
        except Exception as e:                     # noqa: BLE001 - 核对绝不能影响研发
            logger.warning('意图核对异常（按未核对处理）：%s', e)
            record.intent_check = {'checked': False, 'reason': 'exception'}
        if round_gaps:
            record.intent_check = dict(record.intent_check or {})
            record.intent_check['gaps'] = round_gaps

        self.progress_cb('strategy_iter_start', {
            'run_uuid': self.run_uuid, 'round': round_no,
            'hypothesis': record.hypothesis,
        })

        try:
            is_metrics, oos_metrics = self.runner.run_both(
                gen.strategy_json, self._universe(), self._period())
        except Exception as e:
            # 回测失败（数据缺失/资金不足/指标异常等）→ 本轮判 failed 并如实落库；
            # run() 会把真实报错回灌给模型换一轮重试，连续多轮才终止整次研发。
            _hint = ''
            if isinstance(e, ZeroDivisionError):
                # 指标内部除零（RSI/KDJ 在无反向波动 / 一字板窗口最常见），
                # 给出可执行的下一步，模型与用户都看得懂
                _hint = ('（指标在部分区间出现除零：常见于 RSI/KDJ 在无反向波动或'
                         '一字板窗口，可把周期调大或换指标）')
            record.compile_ok = 0
            record.compile_error = f'回测执行失败：{e}{_hint}'
            record.failure_kind = 'backtest'
            record.verdict = 'failed'
            self._persist(record)
            self.progress_cb('strategy_iter_done', {
                'run_uuid': self.run_uuid, 'round': round_no,
                'hypothesis': record.hypothesis, 'error': record.compile_error,
                'verdict': 'failed',
            })
            return record

        record.is_metrics = is_metrics
        record.oos_metrics = oos_metrics
        # 短样本保护：OOS 沿用 Spike 修订 3；IS 同口径（只有 OOS 有门槛时，
        # 「16 根 bar 5 笔交易」的样本内数字仍会被当成有效结果展示）。
        oos_trades = (oos_metrics or {}).get('trades') or 0
        is_trades = (is_metrics or {}).get('trades') or 0
        record.low_sample = 1 if oos_trades < OOS_MIN_TRADES else 0
        if is_metrics is not None:
            is_metrics['low_sample'] = 1 if is_trades < IS_MIN_TRADES else 0
        if oos_metrics is not None:
            oos_metrics['low_sample'] = record.low_sample
        record.overfit_flag = 1 if self.mark_overfit(record) else 0
        # 判定优先级：degenerate（数据不可用/零成交）> reject（过拟合）> keep。
        # 零交易轮过去因为 sharpe=None → mark_overfit 直接 False → keep，
        # 用户看到「保留」而模型也学不到「这轮什么都没发生」（事故 2026-10-18）。
        if round_is_degenerate(is_metrics, oos_metrics):
            record.verdict = 'degenerate'
        elif record.overfit_flag:
            record.verdict = 'reject'
        else:
            record.verdict = 'keep'
        self._persist(record)
        self.progress_cb('strategy_iter_done', _record_payload(record))
        return record

    # ── 终止判定 ──

    def should_stop(self, iters: List[IterRecord]) -> Optional[str]:
        """converged（OOS 夏普连续 2 轮未提升）/ max_rounds / budget / None。"""
        if len(iters) >= MAX_ROUNDS:
            return STOP_MAX_ROUNDS
        if self._budget_exhausted():
            return STOP_BUDGET
        if self._converged(iters):
            return STOP_CONVERGED
        return None

    def _converged(self, iters: List[IterRecord]) -> bool:
        """最近 CONVERGED_STREAK 个有效轮次都未刷新历史最优 OOS 夏普 → 收敛。

        有效轮次：编译通过、非低样本、非退化（零成交/数据不可用）、OOS 夏普可比较
        （短样本保护，Spike 修订 3）。
        """
        valid = [it for it in iters
                 if it.compile_ok and not it.low_sample
                 and not round_is_degenerate(it.is_metrics, it.oos_metrics)
                 and it.oos_sharpe() is not None]
        if len(valid) < CONVERGED_STREAK + 1:
            return False
        # 历史最优（截至倒数第 CONVERGED_STREAK 轮之前）
        prior = valid[:-CONVERGED_STREAK]
        best_prior = max(it.oos_sharpe() for it in prior)
        for it in valid[-CONVERGED_STREAK:]:
            if (it.oos_sharpe() or 0) > best_prior:
                return False
        return True

    def _budget_exhausted(self) -> bool:
        return self.budget is not None and self._llm_calls >= self.budget

    # ── 红线与选优 ──

    def mark_overfit(self, it: IterRecord) -> bool:
        """IS/OOS 夏普比 > 1.3 → 过拟合迹象；OOS ≤0 且 IS>0 也视为过拟合迹象。"""
        is_s, oos_s = it.is_sharpe(), it.oos_sharpe()
        if is_s is None or oos_s is None:
            return False
        if oos_s <= 0:
            return is_s > 0
        return (is_s / oos_s) > self.OVERFIT_RATIO

    def select_best(self, iters: List[IterRecord]) -> Optional[IterRecord]:
        """只看 OOS 夏普；过拟合 / 低样本 / 退化 / **偏离用户原话**的轮次不得当选。

        「赚不赚钱」与「是不是我说的那个策略」是两件事：一个把「上涨买入」实现成
        「下跌反弹买入」的高夏普轮次，对用户是**错误答案**，不能当选。
        """
        candidates = [
            it for it in iters
            if it.compile_ok and not it.overfit_flag and not it.low_sample
            and not round_is_degenerate(it.is_metrics, it.oos_metrics)
            and not (it.is_metrics or {}).get('low_sample')
            and not is_off_intent(it)
            and it.oos_sharpe() is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda it: it.oos_sharpe() or 0)

    # ── 任务上下文（从 db 读回 idea/universe/period，避免构造参数膨胀） ──

    def _idea(self) -> str:
        run = self.db.get_run(self.run_uuid) if hasattr(self.db, 'get_run') else None
        return (run or {}).get('idea', '')

    def _universe(self) -> str:
        run = self.db.get_run(self.run_uuid) if hasattr(self.db, 'get_run') else None
        return (run or {}).get('universe', '')

    def _period(self) -> Tuple[str, str]:
        run = self.db.get_run(self.run_uuid) if hasattr(self.db, 'get_run') else None
        period = (run or {}).get('period', '')
        if '~' in period:
            s, e = period.split('~', 1)
            return s, e
        return '', ''

    # ── 落库 ──

    def _persist(self, record: IterRecord) -> None:
        try:
            record.id = self.db.insert_iter(self.run_uuid, record)
        except Exception:
            logger.exception('[策略研发] 轮次落库失败')


def _record_payload(record: Optional[IterRecord]) -> dict:
    """IterRecord → SSE/前端载荷（四要素：假设/diff/IS/OOS + 判定）。"""
    if record is None:
        return {}
    return {
        'run_uuid': '',
        'round': record.round,
        'id': record.id,
        'hypothesis': record.hypothesis,
        'strategy_diff': record.strategy_diff,
        'is_metrics': record.is_metrics,
        'oos_metrics': record.oos_metrics,
        'overfit_flag': record.overfit_flag,
        'low_sample': record.low_sample,
        'verdict': record.verdict,
        'model': record.model,
        'intent_check': record.intent_check,
    }
