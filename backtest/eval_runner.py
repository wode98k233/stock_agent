"""回测评判（IS/OOS 默认化）：对一次已完成的回测补跑样本内外对照。

依据 docs/superpowers/specs/2026-10-11-backtest-loop-and-conversational-strategy-design.md §7。
复用 backtest/strategy_research.py::IsoOsRunner（通用件：只依赖 strategy_json + universe + period，
不依赖 LLM），结果（两段完整指标 + 切分点 + 过拟合判定）落 backtest_eval 表。

与 backtest/health.py 的分工：
    health   回答「这次回测本身数据够不够、参数合不合理」（Plan 1：看数据覆盖与成交笔数）
    verdict  回答「这个结论经不经得起样本外检验」（本模块：看 IS/OOS 对比）
"""
from __future__ import annotations

import json
import logging
import math
import threading
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

OVERFIT_SHARPE_RATIO = 1.3   # 业务规则：IS/OOS 夏普比 > 1.3 视为过拟合嫌疑，不得当选最优
MIN_EVAL_TRADES = 10         # 两段中较少的一段成交少于这个数 → 统计无意义（与 Plan 1 trade_sample_ok 同口径）


def _num(value: Any) -> Optional[float]:
    """宽松转 float：None / 空串 / 非数字都返回 None（绝不抛）。"""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_eval_verdict(is_metrics: Dict[str, Any], oos_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """纯函数：由两段指标五件套算出过拟合判定。

    判定优先级：样本不足 > 过拟合嫌疑 > 样本外为负 > 通过。
    """
    is_sharpe = _num((is_metrics or {}).get('sharpe'))
    oos_sharpe = _num((oos_metrics or {}).get('sharpe'))
    is_trades = int(_num((is_metrics or {}).get('trades')) or 0)
    oos_trades = int(_num((oos_metrics or {}).get('trades')) or 0)
    eval_trades = min(is_trades, oos_trades)

    ratio = None
    if is_sharpe is not None and oos_sharpe is not None and oos_sharpe > 0:
        ratio = round(is_sharpe / oos_sharpe, 4)

    if eval_trades < MIN_EVAL_TRADES:
        verdict, overfit = 'insufficient_sample', 0
    elif ratio is not None and ratio > OVERFIT_SHARPE_RATIO:
        verdict, overfit = 'overfit_suspect', 1
    elif oos_sharpe is not None and oos_sharpe < 0:
        verdict, overfit = 'oos_negative', 0
    else:
        verdict, overfit = 'ok', 0

    return {
        'is_sharpe': is_sharpe,
        'oos_sharpe': oos_sharpe,
        'sharpe_ratio_io': ratio,
        'overfit_flag': overfit,
        'verdict': verdict,
        'eval_trades': eval_trades,
    }


def _compute_trial_dsr(strategy_json: str, is_metrics: Dict[str, Any],
                       is_run_id: str) -> Dict[str, Any]:
    """trial 记账 + Deflated Sharpe（P1 §4）。

    - DSR 校正对象 = **IS 段夏普**（研发选优发生在 IS）
    - ``sr`` 优先取 IS run 的 ``sr_daily``（per-period 精确值）；缺失时年化五件套
      ÷√252 兜底（per-period 近似）
    - ``sr_std`` 三级降级（§4.1b）；缺失 → DSR None（前端/调用方标注「未校正」）
    """
    from backtest.strategy_research import strategy_hash as _s_hash
    from backtest.statistics import TRADING_DAYS_PER_YEAR, deflated_sharpe_ratio
    from utils.cache.backtest_db import (count_trials_for_strategy, get_backtest_result,
                                         trial_sharpe_std)

    s_hash = _s_hash(strategy_json) if strategy_json else ''
    trial_count = count_trials_for_strategy(s_hash)
    sr_std_annual, sr_std_source = trial_sharpe_std(s_hash)
    sr_std_period = (sr_std_annual / math.sqrt(TRADING_DAYS_PER_YEAR)) if sr_std_annual else None

    res = get_backtest_result(is_run_id) or {}
    sr_daily = _num(res.get('sr_daily'))
    n_obs = int(_num(res.get('data_bars')) or 0)
    skew = _num(res.get('skewness'))
    kurt = _num(res.get('kurtosis'))
    if sr_daily is None:
        s_annual = _num((is_metrics or {}).get('sharpe'))
        sr_daily = (s_annual / math.sqrt(TRADING_DAYS_PER_YEAR)) if s_annual is not None else None

    dsr = None
    if sr_daily is not None and n_obs > 1:
        dsr = deflated_sharpe_ratio(sr_daily, n_obs, skew, kurt, trial_count, sr_std_period)
    return {
        'trial_count': trial_count,
        'sr_std': round(sr_std_annual, 4) if sr_std_annual is not None else None,
        'sr_std_source': sr_std_source,
        'deflated_sharpe': round(dsr, 4) if dsr is not None else None,
    }


def load_strategy_json(strategy_id: str) -> str:
    """取策略 JSON 文本（strategies.code）。取不到返回空串——调用方据此跳过。"""
    if not strategy_id:
        return ''
    try:
        from utils.cache.backtest_db import get_strategy
        row = get_strategy(strategy_id) or {}
        return (row.get('code') or '').strip()
    except Exception as e:
        logger.warning(f'[回测评判] 读取策略失败 {strategy_id}: {e}')
        return ''


def run_isoos_eval(run_id: str, strategy_json: str, universe: str,
                   period: Tuple[str, str], *, initial_cash: float = 1000000.0,
                   entry_timing: str = 'next_open', runner: Any = None,
                   store: Any = None) -> Dict[str, Any]:
    """同步执行一次样本内外评判并落库（后台线程与单测共用这条路径）。"""
    from backtest.strategy_research import IsoOsRunner
    from utils.cache.backtest_db import save_backtest_eval

    if runner is None:
        runner = IsoOsRunner(initial_cash=initial_cash, entry_timing=entry_timing,
                             origin='eval', ref_id=run_id)
    is_metrics, oos_metrics = runner.run_both(strategy_json, universe, tuple(period))
    split = runner.split_period(period[0], period[1], universe)
    payload = {
        'is_metrics_json': json.dumps(is_metrics, ensure_ascii=False),
        'oos_metrics_json': json.dumps(oos_metrics, ensure_ascii=False),
        'split_date': split[0][1],
    }
    payload.update(compute_eval_verdict(is_metrics, oos_metrics))
    # P1 §4：trial 记账 + Deflated Sharpe（IS 段 = 研发选优对象；回填被评判的主 run）
    try:
        from backtest.strategy_research import strategy_hash as _s_hash
        is_run_id = f'SR-IS-sr_{_s_hash(strategy_json)}' if strategy_json else ''
        dsr_info = _compute_trial_dsr(strategy_json, is_metrics, is_run_id)
        payload.update(dsr_info)
        from utils.cache.backtest_db import update_result_dsr
        update_result_dsr(run_id, dsr_info.get('deflated_sharpe'), dsr_info.get('trial_count'))
    except Exception as e:  # noqa: BLE001 - 统计增强失败不阻塞评判主流程
        logger.warning(f'[回测评判] {run_id} trial/DSR 计算失败（不阻塞）: {e}')
    (store or save_backtest_eval)(run_id, payload)
    return payload


def should_auto_eval(health: str, origin: str = 'manual') -> bool:
    """该不该自动补跑评判。

    规则：开关打开 + 不是评判自己产生的 run（否则递归）+ 健康态允许（ok / low_sample）。
    data_gap / no_signal / invalid_params / error 不评——数据都不够或没成交，评出来的 IS/OOS 毫无意义。
    """
    try:
        from config import Config
        if not getattr(Config, 'BACKTEST_AUTO_EVAL', True):
            return False
    except Exception:
        pass
    if (origin or 'manual') == 'eval':
        return False
    return (health or 'ok') in ('ok', 'low_sample')


def schedule_isoos_eval(run_row: Dict[str, Any], result_row: Dict[str, Any]) -> Optional[threading.Thread]:
    """在后台线程补跑评判并立即返回（不阻塞主回测返回）。"""
    run_id = (run_row or {}).get('run_id')
    if not run_id:
        return None
    health = (result_row or {}).get('health') or 'ok'
    origin = (run_row or {}).get('origin') or 'manual'
    if not should_auto_eval(health, origin):
        logger.info(f'[回测评判] {run_id} 跳过自动评判（health={health} origin={origin}）')
        return None
    strategy_json = load_strategy_json((run_row or {}).get('strategy_id') or '')
    if not strategy_json:
        logger.info(f'[回测评判] {run_id} 无策略 JSON（内置 Python 策略），跳过自动评判')
        return None
    thread = threading.Thread(
        target=_eval_worker,
        args=(run_id, strategy_json, run_row.get('code'),
              (run_row.get('start_date'), run_row.get('end_date'))),
        kwargs={'initial_cash': float(run_row.get('initial_cash') or 1000000.0),
                'entry_timing': run_row.get('entry_timing') or 'next_open'},
        daemon=True, name=f'backtest-eval-{run_id}')
    thread.start()
    return thread


def _eval_worker(run_id: str, strategy_json: str, universe: Optional[str],
                 period: Tuple[Optional[str], Optional[str]], *, initial_cash: float,
                 entry_timing: str) -> None:
    """后台线程体：绝不抛异常（线程里抛出会静默丢失）。"""
    if not universe or not period[0] or not period[1]:
        logger.warning(f'[回测评判] {run_id} 缺少标的或区间，跳过')
        return
    try:
        payload = run_isoos_eval(run_id, strategy_json, universe, period,
                                 initial_cash=initial_cash, entry_timing=entry_timing)
        logger.info(f'[回测评判] {run_id} verdict={payload.get("verdict")} '
                    f'is_sharpe={payload.get("is_sharpe")} oos_sharpe={payload.get("oos_sharpe")}')
    except Exception as e:
        logger.warning(f'[回测评判] {run_id} 评判失败: {e}')
        try:
            from utils.cache.backtest_db import save_backtest_eval
            save_backtest_eval(run_id, {'verdict': 'failed', 'overfit_flag': 0,
                                        'error_message': str(e)[:500]})
        except Exception:
            logger.exception(f'[回测评判] {run_id} 失败状态回写也失败')
