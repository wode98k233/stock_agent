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
CONVERGED_STREAK = 2           # OOS 夏普连续 N 轮未提升 → 收敛终止（AC-5）
LLM_PACING_SECONDS = 20.0      # LLM 调用最小间隔（Spike 第二轮实测参数）
LLM_BACKOFF_SECONDS = 30.0     # 429/限流退避秒数
MAX_GEN_RETRIES = 2            # 非法输出的错误反馈重试上限（初次 + 2 次重试 = 3 次机会）

DISCLAIMER = '历史表现不代表未来收益，本结果仅为研究结论，不构成投资建议'

# 文案红线敏感词（AC-8）：prompt 指令禁用 + 输出扫描过滤
SENSITIVE_WORDS: Tuple[str, ...] = ('稳赚', '必胜', '躺赢', '包赚', '稳赢')

# 终止原因（strategy_run.stop_reason）
STOP_CONVERGED = 'converged'
STOP_MAX_ROUNDS = 'max_rounds'
STOP_BUDGET = 'budget'
STOP_FAILED = 'failed'
STOP_CANCELLED = 'cancelled'

# 全局 LLM 调用节奏（跨 Generator 实例共享，保证多轮循环串行调用时也遵守间隔）
_llm_pacing_lock = threading.Lock()
_last_llm_ts: float = 0.0


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
    overfit_flag: int = 0
    low_sample: int = 0               # OOS trades < OOS_MIN_TRADES（不参与最优比较）
    verdict: str = ''                 # keep / reject / best / failed
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
    """

    def __init__(self, llm: Any = None,
                 pacing_seconds: float = LLM_PACING_SECONDS,
                 backoff_seconds: float = LLM_BACKOFF_SECONDS,
                 max_retries: int = MAX_GEN_RETRIES,
                 llm_logger: Any = None):
        self._llm = llm
        self.pacing_seconds = pacing_seconds
        self.backoff_seconds = backoff_seconds
        self.max_retries = max_retries
        self.logger = llm_logger or logger

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

    def _invoke_llm(self, messages: List[Tuple[str, str]]) -> str:
        """带节奏与 429 退避的 LLM 调用。返回原始文本；失败抛出异常。"""
        llm = self._get_llm()
        lc_messages = _to_langchain_messages(messages)
        last_exc: Optional[Exception] = None
        for attempt in range(3):  # 网关层之上最多 3 次（含退避）
            self._pacing(extra=self.backoff_seconds if attempt else 0.0)
            try:
                resp = llm.invoke(lc_messages)
                content = getattr(resp, 'content', '') or ''
                model_name = getattr(getattr(resp, 'response_metadata', {}) or {}, 'model_name', '') \
                    if isinstance(getattr(resp, 'response_metadata', None), dict) else ''
                self._model_name = model_name or getattr(llm, 'model', '') or ''
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
            '策略 JSON 必须完全符合下方 schema；严禁使用「稳赚/必胜/躺赢/包赚/稳赢」等承诺收益的词汇。\n\n'
            + export_llm_schema()
        )

    def _initial_user_prompt(self, idea: str, universe: str) -> str:
        return (
            f'研究想法：{idea}\n'
            f'回测标的：{universe}（单标的日级回测）\n\n'
            '请设计一个与想法匹配的策略，输出 {"hypothesis": ..., "strategy": ...}。'
            'hypothesis 用一句话说明为什么该策略可能有效。'
        )

    def _mutate_user_prompt(self, idea: str, universe: str,
                            history: List[IterRecord]) -> str:
        lines = [f'研究想法：{idea}', f'回测标的：{universe}', '', '历史迭代轮次：']
        for it in history:
            is_m = it.is_metrics or {}
            oos_m = it.oos_metrics or {}
            lines.append(
                f"- 第{it.round}轮 假设:{it.hypothesis or '（无）'} | "
                f"IS: cagr={is_m.get('cagr')}, sharpe={is_m.get('sharpe')}, mdd={is_m.get('mdd')} | "
                f"OOS: cagr={oos_m.get('cagr')}, sharpe={oos_m.get('sharpe')}, "
                f"mdd={oos_m.get('mdd')}, trades={oos_m.get('trades')} | "
                f"过拟合:{'是' if it.overfit_flag else '否'} | 判定:{it.verdict}"
            )
        lines += [
            '',
            '请基于以上全部轮次结果，提出一个可能改善 OOS（样本外）表现的新策略变体：',
            '- 优先调整样本外表现差的结构（如回撤过大、交易过少），而不是照抄上一轮；',
            '- 只输出 {"hypothesis": "本轮假设", "strategy": {变异后的完整策略 JSON}}。',
        ]
        return '\n'.join(lines)

    # ── 生成入口 ──

    def generate_initial(self, idea: str, universe: str) -> GenResult:
        """想法 → 策略 JSON + 假设。"""
        messages = [
            ('system', self._system_prompt()),
            ('user', self._initial_user_prompt(idea, universe)),
        ]
        return self._gen_with_retry(messages)

    def mutate(self, idea: str, history: List[IterRecord]) -> GenResult:
        """基于历史轮次摘要变异（DECIDE 节点上下文不含 K 线明细，防 token 爆炸）。"""
        messages = [
            ('system', self._system_prompt()),
            ('user', self._mutate_user_prompt(idea, '', history)),
        ]
        return self._gen_with_retry(messages)

    def _gen_with_retry(self, messages: List[Tuple[str, str]],
                        max_retries: Optional[int] = None) -> GenResult:
        """生成 + 编译校验 + 错误反馈重试（≤ max_retries 次）。

        非法 JSON / 编译失败 → 把错误信息（含 compiler ValueError 的支持清单）
        作为反馈消息重试；连续失败返回 ok=False + 可读原因（不暴露堆栈）。
        """
        retries = self.max_retries if max_retries is None else max_retries
        feedback: List[Tuple[str, str]] = []
        last_error = ''
        attempts = 0
        model_name = ''

        for attempt in range(retries + 1):
            attempts += 1
            try:
                raw = self._invoke_llm(messages + feedback)
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

            # 编译校验（唯一校验门；错误信息自带支持清单）
            try:
                from backtest.json_compiler import compile_strategy
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

            return GenResult(ok=True, strategy_json=strategy_str,
                             hypothesis=hypothesis or '（未提供假设）',
                             model=model_name, attempts=attempts)

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
                 engine: Any = None, benchmark: str = ''):
        self.split_ratio = split_ratio
        self.initial_cash = initial_cash
        self._engine = engine          # 惰性取 BacktestEngine（可注入替身）
        self.benchmark = benchmark
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

    def _run_segment(self, strategy_id: str, universe: str,
                     start: str, end: str, label: str) -> dict:
        """单段回测并提取指标五件套；失败抛出（由状态机决定任务失败）。"""
        config = {
            'code': universe,
            'strategy_id': strategy_id,
            'strategy_name': strategy_id,
            'start_date': start,
            'end_date': end,
            'initial_cash': self.initial_cash,
            'params': {},
            'adjust_type': 'qfq',
            'benchmark': self.benchmark,
        }
        logger.info(f'[策略研发] {label} 回测: {universe} {start}~{end}')
        result = self._get_engine().run(dict(config), run_id=f'SR-{label}-{strategy_id}')
        return {
            'cagr': result.get('annual_return'),
            'sharpe': result.get('sharpe_ratio'),
            'mdd': result.get('max_drawdown'),
            'win_rate': result.get('win_rate'),
            'trades': result.get('trade_count'),
        }


# ============================================================
# 落库适配器（包装 utils.cache.backtest_db 的 CRUD，可注入替身）
# ============================================================


class BacktestRunStore:
    """StrategyResearchLoop 的 strategy_run / strategy_iter 读写适配。"""

    def create_run(self, run_uuid: str, idea: str, universe: str, period: str,
                   source: str = 'panel', dialog_uuid: Optional[str] = None) -> None:
        from utils.cache.backtest_db import create_strategy_run
        create_strategy_run(run_uuid, idea, universe, period,
                            source=source, dialog_uuid=dialog_uuid)

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
            model=record.model,
        )

    def set_iter_verdict(self, iter_id: int, verdict: str) -> None:
        from utils.cache.backtest_db import update_strategy_iter
        update_strategy_iter(iter_id, verdict=verdict)


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
        self.progress_cb = progress_cb or (lambda _e, _d: None)
        self.budget = budget
        self.cancel_check = cancel_check or (lambda: False)
        self._llm_calls = 0

    # ── 主循环 ──

    def run(self) -> RunResult:
        """主循环：逐轮执行直至终止条件命中；结束后选优落库。"""
        history: List[IterRecord] = []
        status = 'done'
        stop_reason = STOP_MAX_ROUNDS

        for round_no in range(1, MAX_ROUNDS + 1):
            if self.cancel_check():
                status, stop_reason = 'cancelled', STOP_CANCELLED
                break
            if self._budget_exhausted():
                status, stop_reason = 'done', STOP_BUDGET
                break

            record = self.step(round_no, history)
            history.append(record)

            if record.verdict == 'failed':
                status, stop_reason = 'failed', STOP_FAILED
                break

            stop = self.should_stop(history)
            if stop:
                status = 'done'
                stop_reason = stop
                break

        best = self.select_best(history) if status == 'done' else None
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
            gen = self.generator.generate_initial(self._idea(), self._universe())
        else:
            gen = self.generator.mutate(self._idea(), history)

        if not gen.ok:
            # 连续 3 次非法 → 轮次 failed（整任务终止，PRD FR-02 / AC-3）
            record = IterRecord(
                round=round_no, hypothesis='', strategy_json=None,
                compile_ok=0, compile_error=gen.error, verdict='failed',
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

        self.progress_cb('strategy_iter_start', {
            'run_uuid': self.run_uuid, 'round': round_no,
            'hypothesis': record.hypothesis,
        })

        try:
            is_metrics, oos_metrics = self.runner.run_both(
                gen.strategy_json, self._universe(), self._period())
        except Exception as e:
            # 回测失败（数据缺失/资金不足等）→ 该轮拒绝，任务终止并给出可读原因
            record.compile_ok = 0
            record.compile_error = f'回测执行失败：{e}'
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
        # OOS 短样本保护（Spike 修订 3）
        oos_trades = (oos_metrics or {}).get('trades') or 0
        record.low_sample = 1 if oos_trades < OOS_MIN_TRADES else 0
        record.overfit_flag = 1 if self.mark_overfit(record) else 0
        record.verdict = 'reject' if record.overfit_flag else 'keep'
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

        有效轮次：编译通过、非低样本、OOS 夏普可比较（短样本保护，Spike 修订 3）。
        """
        valid = [it for it in iters
                 if it.compile_ok and not it.low_sample and it.oos_sharpe() is not None]
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
        """只看 OOS 夏普；overfit_flag=1 与低样本轮次不得当选（红线 G3 + Spike 修订 3）。"""
        candidates = [
            it for it in iters
            if it.compile_ok and not it.overfit_flag and not it.low_sample
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
    }
