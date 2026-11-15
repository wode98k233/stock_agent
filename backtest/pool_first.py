"""池子优先验证 + 组合回测（MVP）。

横截面：同一份策略 JSON 逐只独立回测，每只 100 万，聚合用 pool_study 的三档口径。
组合：用逐只交易的买卖日/价做共享资金叠加（最多 N 个持仓、等权、T+1）。
不写业务库。
"""
from __future__ import annotations

import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date
from typing import Any, Dict, Iterable, List, Tuple

import backtrader as bt

from backtest.a_stock_broker import setup_broker
from backtest.batch_engine import _auto_detect_asset_type
from backtest.data_adapter import BacktraderDataFeed
from backtest.json_compiler import compile_strategy
from backtest.pool_study import StockResult, build_caveats, summarize, verdict_of

DEFAULT_STOCKS = [
    '600519', '601318', '600900', '601899', '600276', '000725', '002179',
    '002466', '002611', '002657', '300750', '601595', '601857', '001330',
    '000001', '600000', '600036', '000333', '002415', '601166', '600030',
    '000651', '002594', '300059', '600887',
]
DEFAULT_ETFS = [
    '510300', '510500', '510050', '159919', '159915', '588000', '512880',
    '512690', '512010', '515030', '510880', '513100', '518880', '511010',
    '511260',
]
DEFAULT_POOL = DEFAULT_STOCKS + DEFAULT_ETFS
# 验证/烟测专用：15 只混合池（8 股票 + 7 ETF），只用于跑通模式，不当真实结论。
SMOKE_STOCKS = ['600519', '601318', '300750', '002179', '000725', '601899', '600036', '000333']
SMOKE_ETFS = ['510300', '159915', '512880', '512690', '513100', '518880', '511010']
SMOKE_POOL = SMOKE_STOCKS + SMOKE_ETFS
POOL_ALIASES = {'default', 'pool', 'default_pool', '默认', '默认池'}
SMOKE_ALIASES = {'smoke', 'smoke_pool', '验证池', '烟测池'}


def material_deviations(intent_check: Dict[str, Any]) -> List[str]:
    """过滤掉「成交口径没写进 JSON」这类假偏离：口径由引擎 timing 参数表达。"""
    terms = ('same_close', 'next_open', 'next_close', '尾盘', '次日开盘',
             '成交口径', '持有 1 根', '持有1根', '持有 1根')
    out = []
    for d in ((intent_check or {}).get('deviations') or []):
        text = str(d)
        if not any(t in text for t in terms):
            out.append(text)
    return out


def _sweep_context(sweep_holds: Any = None, compare_exit: bool = False) -> str:
    """给意图核对器的「引擎额外对比」说明（见 intent_fidelity.check_intent）。"""
    parts: List[str] = []
    holds = []
    for h in (sweep_holds or []):
        try:
            n = int(h)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 60 and n not in holds:
            holds.append(n)
    if len(holds) >= 2:
        parts.append('同一策略会分别用持有 ' + '/'.join(str(h) for h in sorted(holds))
                     + ' 根回测（只改 conditions.sell.hold_bars）')
    if compare_exit:
        parts.append('同一策略会分别按次日开盘卖出 / 次日收盘卖出各跑一遍')
    return '；'.join(parts)


# 意图核对器只看得到策略 JSON（买卖条件）。标的池 / 区间 / 成交时点都是引擎参数，
# 不告诉它就会被判成「未体现标的池与区间」的偏离（真机 2026-10-25 二次误报）。
_ENGINE_SIDE_NOTE = (
    '标的池、回测区间、买卖成交时点（次日开盘/尾盘等）都由引擎参数决定，'
    '策略 JSON 只描述买卖条件；不要因为它们不在 JSON 里就判偏离')


def _concept_context(idea: str) -> str:
    """「深V/超跌反弹」是概念，不是用户给的阈值：价格化映射算约定，不算偏离。

    不说明这一点，核对器会报「用户未明确 3% 阈值」并触发重试，重试按「不要加用户
    没提的约束」可能把下探条件整个删掉 —— 那就不再是深V 策略（真机 2026-10-25）。
    """
    text = str(idea or '').lower()
    if not any(k in text for k in ('深v', '深 v', '超跌', '下探', '探底', '大跌',
                                   '急跌', '暴跌', '反弹', '振幅', '收回', '长下影')):
        return ''
    return ('原话里的「深V / 超跌反弹 / 振幅」是自然语言概念，引擎用价格条件表达'
            '（low < mul(pre_close,k)、close > open、high > mul(low,k)）；'
            '阈值由系统推断，**不算偏离**；用户明确给了阈值时必须以用户的为准')


def generate_pool_strategy(idea: str, pool_desc: str, *,
                           entry_timing: str = 'same_close',
                           exit_timing: str = 'next_open',
                           max_retries: int = 1,
                           sweep_holds: Any = None,
                           compare_exit: bool = False) -> Dict[str, Any]:
    """池子模式生成：不写死单票涨停上限；偏离原话时带反馈重试一次。"""
    from backtest.strategy_research import StrategyGenerator
    gen = StrategyGenerator(entry_timing=entry_timing, exit_timing=exit_timing,
                            pacing_seconds=0.0, pool_mode=True, pool_desc=pool_desc)
    res = gen.generate_for_pool(idea, pool_desc)
    if not res.ok:
        return {'ok': False, 'error': str(res.error or '策略生成失败')}

    # 持有期矩阵 / 卖出口径对比是引擎参数，不在策略 JSON 里：
    # 不把这件事告诉核对器，它会误报「策略固定持有 4 根，未按用户要求对比 3/4/5」
    # （真机 2026-10-25，用户因此认为策略与提问不搭配）。
    extra_context = '；'.join(x for x in (_ENGINE_SIDE_NOTE,
                                         _sweep_context(sweep_holds, compare_exit),
                                         _concept_context(idea)) if x)
    intent_check: Dict[str, Any] = {}
    # ⛔ 这里**不许**再写死「收盘涨幅≥5%、持有 1 根」这类描述：
    # 那不是用户说的话。真机 2026-10-24：用户要的是「深V 反弹后次日开盘买」，生成器
    # 给出了「涨幅≥5% 买入」，而假基准恰好也是「涨幅≥5%」，核对反而认为买点一致，
    # 只报了持有期/deviation —— 基准造假让整个意图闸门失效。
    # 买点/卖点留空时 check_intent 会显式标「（未明确）」，并主要依据用户原话判断。
    try:
        from backtest.intent_fidelity import check_intent
        intent_check = check_intent(idea=idea, strategy_json=res.strategy_json,
                                    logger=gen.logger,
                                    extra_context=extra_context) or {}
    except Exception:  # noqa: BLE001 - 核对失败 fail-open，不阻断池子
        intent_check = {'checked': False, 'reason': 'exception'}

    if (intent_check.get('checked') and intent_check.get('matches') is False
            and material_deviations(intent_check)
            and int(max_retries) > 0):
        devs = material_deviations(intent_check)[:4]
        feedback = (
            '上一版偏离了用户原话：' + ('；'.join(devs) if devs else '（见核对结论）') +
            '。请只保留用户原话里的条件：买卖条件、买卖时点、持有天数；'
            '**不要添加量能放大、开盘/收盘比较、单票涨停上限**（涨停由 broker 按标的拦截）。'
            '若原话是「深V / 超跌反弹 / 某日振幅很大」，用价格条件表达：'
            'low 低于前收（low < mul(pre_close, 阈值)）、收盘收回（close > open）、'
            '振幅（high > mul(low, 阈值)），**不要用 pct_change 顶替**。'
        )
        gen._messages.append(('user', feedback))
        retry = gen._gen_with_retry(label='池子修正')
        if retry.ok:
            res = retry
            try:
                intent_check = check_intent(idea=idea, strategy_json=res.strategy_json,
                                            logger=gen.logger,
                                            extra_context=extra_context) or intent_check
            except Exception:  # noqa: BLE001
                pass
    return {'ok': True, 'strategy_json': res.strategy_json,
            'hypothesis': res.hypothesis, 'intent_check': intent_check}


def _scope_label(spec: str) -> str:
    """范围的人话标签（仅用于展示；解析失败退回 spec 本身，不影响回测）。"""
    try:
        from backtest.pool_scope import describe_scope
        return describe_scope(spec)
    except Exception:  # noqa: BLE001
        return str(spec or '')


def resolve_pool(spec: str) -> List[str]:
    raw = str(spec or '').strip()
    if not raw:
        return list(DEFAULT_POOL)
    upper = raw.upper()
    if upper in {'POOL:SMOKE', 'SMOKE', 'SMOKE_POOL'} or raw in SMOKE_ALIASES:
        return list(SMOKE_POOL)
    if upper in {'POOL', 'POOL:DEFAULT', 'DEFAULT', 'DEFAULT_POOL'} or raw in POOL_ALIASES:
        return list(DEFAULT_POOL)
    if upper.startswith('POOL:'):
        # 新增范围 spec（指数成分 / 自选 / 持仓 / 板块 / 代码池 / limit|kind）
        # 交给 pool_scope 解析；老的 POOL:default / POOL:smoke / POOL:代码列表 行为不变。
        from backtest.pool_scope import is_scope_spec, resolve_scope
        if is_scope_spec(raw):
            return list(resolve_scope(raw)['codes'])
        body = raw.split(':', 1)[1].strip()
        if body.lower() in SMOKE_ALIASES:
            return list(SMOKE_POOL)
        if not body or body.lower() in POOL_ALIASES:
            return list(DEFAULT_POOL)
        codes = [c.strip() for c in body.split(',') if c.strip()]
    else:
        codes = [c.strip() for c in raw.split(',') if c.strip()]
    out, seen = [], set()
    for c in codes:
        if len(c) == 6 and c.isdigit() and c not in seen:
            seen.add(c)
            out.append(c)
    if not out:
        raise ValueError(f'池子为空或代码不合法: {spec}')
    return out


def _net_pct(buy: Dict[str, Any], sell: Dict[str, Any]) -> float:
    cost = (buy.get('price') or 0) * (buy.get('quantity') or 0)
    net = (sell.get('pnl') or 0) - (buy.get('commission') or 0) - (sell.get('commission') or 0)
    return (net / cost * 100) if cost > 0 else 0.0


def _run_one(code: str, strategy_json: str, start: str, end: str,
             entry_timing: str, exit_timing: str) -> Dict[str, Any]:
    cls = compile_strategy(strategy_json)
    data = BacktraderDataFeed.from_stock_daily(code, start, end)
    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(cls, entry_timing=entry_timing, exit_timing=exit_timing)
    # 与单票引擎同款口径：按日收益、年化、rf=0（默认 Years 会算不出来 → None）
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe',
                        riskfreerate=0.0, timeframe=bt.TimeFrame.Days, annualize=True)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    setup_broker(cerebro, {
        'code': code, 'asset_type': _auto_detect_asset_type(code),
        'initial_cash': 1_000_000, 'slippage': 0.001, 'price_limit_mode': 'enforce',
    })
    st = cerebro.run()[0]
    trades = st.get_trades()
    sharpe_raw = st.analyzers.sharpe.get_analysis().get('sharperatio')
    sharpe = round(float(sharpe_raw), 3) if isinstance(sharpe_raw, (int, float)) else None
    dd_pct = float(((st.analyzers.drawdown.get_analysis().get('max') or {})
                   .get('drawdown') or 0))
    max_dd = round(-dd_pct / 100.0, 4)
    wins = losses = neutral = 0
    sell_trades = [t for t in trades
                   if t.get('direction') == 'sell' and t.get('pnl') is not None]
    win_pnl = sum(float(t['pnl']) for t in sell_trades if float(t['pnl']) > 0)
    loss_pnl = abs(sum(float(t['pnl']) for t in sell_trades if float(t['pnl']) < 0))
    profit_factor = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else None
    holds = [float(t['holding_days']) for t in sell_trades
             if t.get('holding_days') is not None]
    avg_hold_days = round(statistics.mean(holds), 1) if holds else None
    for i in range(0, len(trades) - 1):
        b, s = trades[i], trades[i + 1]
        if b['direction'] != 'buy' or s['direction'] != 'sell':
            continue
        pct = _net_pct(b, s)
        if pct > 0.1:
            wins += 1
        elif pct < -0.1:
            losses += 1
        else:
            neutral += 1
    effective = wins + losses
    sells = sum(1 for t in trades if t['direction'] == 'sell')
    ret = cerebro.broker.getvalue() / 1_000_000 - 1
    health = 'ok'
    if effective < 5:
        health = 'insufficient_sample'
    if len(data) < 250:
        health = 'insufficient_bars'
    if sells == 0:
        health = 'no_signal'
    return {
        'code': code, 'name': code, 'asset': _auto_detect_asset_type(code),
        'health': health, 'trades': sells, 'effective': effective,
        'win_rate': (wins / effective) if effective else None,
        'neutral_rate': (neutral / (wins + losses + neutral)) if (wins + losses + neutral) else 0.0,
        'total_return': ret, 'bars': len(data),
        'sharpe': sharpe, 'max_drawdown': max_dd,
        'profit_factor': profit_factor, 'avg_hold_days': avg_hold_days,
        'trades_detail': trades,
    }


def evaluate_pool(strategy_json: str, codes: Iterable[str], *,
                  start: str, end: str, entry_timing: str = 'same_close',
                  exit_timing: str = 'next_open', workers: int = 4,
                  progress_cb: Any = None) -> Dict[str, Any]:
    codes = list(dict.fromkeys(str(c).strip() for c in codes if str(c).strip()))
    rows: List[StockResult] = []
    details: Dict[str, List[Dict[str, Any]]] = {}
    errors: List[Dict[str, str]] = []
    done = ok = fail = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futs = {pool.submit(_run_one, c, strategy_json, start, end,
                            entry_timing, exit_timing): c for c in codes}
        for f in as_completed(futs):
            code = futs[f]
            done += 1
            try:
                r = f.result()
                rows.append(StockResult(
                    code=r['code'], name=r['name'], health=r['health'],
                    trades=r['trades'], win_rate=r['win_rate'],
                    total_return=r['total_return'], bars=r['bars'],
                    sharpe=r.get('sharpe'), max_drawdown=r.get('max_drawdown'),
                    profit_factor=r.get('profit_factor'),
                    avg_hold_days=r.get('avg_hold_days'),
                    neutral_rate=r['neutral_rate']))
                details[code] = r['trades_detail']
                ok += 1
            except Exception as e:  # noqa: BLE001
                rows.append(StockResult(code=code, health='error', error=str(e)[:160]))
                errors.append({'code': code, 'error': str(e)[:160]})
                fail += 1
            if progress_cb:
                try:
                    progress_cb(done, len(codes), ok, fail)
                except Exception:  # noqa: BLE001
                    pass
    overall = summarize(rows)
    return {
        'results': rows, 'trades_by_code': details, 'errors': errors,
        'overall': overall, 'verdict': verdict_of(overall),
    }


def _close_prices(codes: List[str], start: str, end: str) -> Dict[str, Dict[str, float]]:
    from utils.cache.market_data_db import get_market_data_db
    out: Dict[str, Dict[str, float]] = {c: {} for c in codes}
    with get_market_data_db() as conn:
        for i in range(0, len(codes), 400):
            chunk = codes[i:i + 400]
            marks = ','.join('?' * len(chunk))
            rows = conn.execute(
                f'SELECT code, trade_date, close FROM stock_daily '
                f'WHERE code IN ({marks}) AND trade_date >= ? AND trade_date <= ?',
                (*chunk, start, end)).fetchall()
            for r in rows:
                out.setdefault(str(r['code']), {})[str(r['trade_date'])] = float(r['close'])
    return out


def simulate_portfolio(trades_by_code: Dict[str, List[Dict[str, Any]]], *,
                       start: str, end: str, initial_cash: float = 1_000_000,
                       max_positions: int = 10, commission: float = 0.0003,
                       stamp_tax: float = 0.0005) -> Dict[str, Any]:
    codes = sorted(trades_by_code)
    closes = _close_prices(codes, start, end)
    buy_events: Dict[str, List[Dict[str, Any]]] = {}
    sell_events: Dict[str, List[Dict[str, Any]]] = {}
    for code, trades in trades_by_code.items():
        pending = None
        for t in trades:
            if t['direction'] == 'buy':
                pending = t
                buy_events.setdefault(t['trade_date'], []).append({
                    'code': code, 'price': float(t['price'])})
            elif t['direction'] == 'sell' and pending:
                sell_events.setdefault(t['trade_date'], []).append({
                    'code': code, 'price': float(t['price']),
                    'buy_price': float(pending['price']),
                    'buy_date': str(pending.get('trade_date') or ''),
                })
                pending = None
    calendar = sorted({d for m in closes.values() for d in m} |
                      set(buy_events) | set(sell_events))
    cash = float(initial_cash)
    alloc = cash / max(1, max_positions)
    positions: Dict[str, Dict[str, Any]] = {}
    last_close: Dict[str, float] = {}
    equity_curve: List[Tuple[str, float]] = []
    closed_pnls: List[float] = []
    hold_days_list: List[int] = []
    recent_trades: List[Dict[str, Any]] = []
    buys = sells = 0
    for day in calendar:
        for ev in sell_events.get(day, []):
            code = ev['code']
            pos = positions.pop(code, None)
            if not pos:
                continue
            qty = pos['qty']
            gross = qty * ev['price']
            fee = gross * commission
            tax = gross * stamp_tax if _auto_detect_asset_type(code) != 'etf' else 0.0
            cash += gross - fee - tax
            net = gross - fee - tax - qty * pos['buy_price']
            closed_pnls.append(net)
            buy_day = str(ev.get('buy_date') or '')
            hold_days = None
            if buy_day:
                try:
                    hold_days = max(0, (date.fromisoformat(day)
                                       - date.fromisoformat(buy_day)).days)
                except ValueError:
                    hold_days = None
            if hold_days is not None:
                hold_days_list.append(hold_days)
            cost = qty * pos['buy_price']
            recent_trades.append({
                'code': code, 'buy_date': buy_day, 'sell_date': day,
                'buy_price': round(pos['buy_price'], 2),
                'sell_price': round(float(ev['price']), 2),
                'pnl': round(net, 2),
                'pnl_pct': round((net / cost * 100) if cost > 0 else 0, 2),
                'hold_days': hold_days, 'quantity': qty,
            })
            sells += 1
        for code, by_day in closes.items():
            if day in by_day:
                last_close[code] = by_day[day]
        for ev in sorted(buy_events.get(day, []), key=lambda x: x['code']):
            code = ev['code']
            if code in positions or len(positions) >= max_positions:
                continue
            price = ev['price']
            budget = min(alloc, cash)
            qty = int(budget / (price * (1 + commission)) / 100) * 100
            if qty < 100:
                continue
            gross = qty * price
            fee = gross * commission
            if gross + fee > cash:
                continue
            cash -= gross + fee
            positions[code] = {'qty': qty, 'buy_price': price}
            buys += 1
        equity = cash + sum(p['qty'] * last_close.get(c, p['buy_price'])
                            for c, p in positions.items())
        equity_curve.append((day, equity))

    open_value = sum(p['qty'] * last_close.get(c, p['buy_price'])
                     for c, p in positions.items())
    final_equity = cash + open_value
    total_return = final_equity / initial_cash - 1
    days = max(1, (date.fromisoformat(end) - date.fromisoformat(start)).days)
    cagr = (final_equity / initial_cash) ** (365 / days) - 1 if final_equity > 0 else -1
    peak = -math.inf
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, eq / peak - 1)
    wins = sum(1 for p in closed_pnls if p > 0)
    win_sum = sum(p for p in closed_pnls if p > 0)
    loss_sum = abs(sum(p for p in closed_pnls if p < 0))
    profit_factor = round(win_sum / loss_sum, 2) if loss_sum > 0 else None
    avg_hold_days = round(statistics.mean(hold_days_list), 1) if hold_days_list else None
    eq_vals = [v for _, v in equity_curve]
    daily_rets = [eq_vals[i] / eq_vals[i - 1] - 1 for i in range(1, len(eq_vals))
                  if eq_vals[i - 1] > 0]
    sharpe = None
    if len(daily_rets) >= 20:
        mu = statistics.mean(daily_rets)
        sd = statistics.pstdev(daily_rets)
        if sd > 0:
            sharpe = round(mu / sd * math.sqrt(252), 2)
    calmar = round(cagr / abs(max_dd), 2) if max_dd < 0 and cagr else None
    return {
        'initial_cash': initial_cash, 'final_equity': round(final_equity, 2),
        'total_return': round(total_return, 4), 'cagr': round(cagr, 4),
        'max_drawdown': round(max_dd, 4), 'buys': buys, 'sells': sells,
        'open_positions': len(positions), 'closed_trades': len(closed_pnls),
        'win_rate': round(wins / len(closed_pnls), 4) if closed_pnls else None,
        'sharpe': sharpe, 'profit_factor': profit_factor,
        'avg_hold_days': avg_hold_days, 'calmar': calmar,
        'recent_trades': recent_trades[-20:],
        'max_positions': max_positions,
    }


def run_pool_first(strategy_json: str, *, pool_spec: str = 'POOL:default',
                   start: str = '2024-01-01', end: str = '2026-10-20',
                   entry_timing: str = 'same_close', exit_timing: str = 'next_open',
                   workers: int = 4, max_positions: int = 10,
                   initial_cash: float = 1_000_000,
                   with_portfolio: bool = True,
                   mode: str = 'both',
                   progress_cb: Any = None) -> Dict[str, Any]:
    """池子验证 + 可选组合。

    mode:
      - validate：只做逐只独立验证（**不共享资金**，默认验证口径）
      - portfolio：只做共享资金的组合叠加
      - both：两者都给（默认，便于同一张报告里对照）
    """
    mode = str(mode or 'both').strip().lower()
    if mode not in ('validate', 'portfolio', 'both'):
        mode = 'both'
    if mode == 'validate':
        with_portfolio = False
    elif mode == 'portfolio':
        with_portfolio = True
    codes = resolve_pool(pool_spec)

    def _pc(done, total, ok, fail):
        if progress_cb:
            try:
                progress_cb('pool_progress', {'done': done, 'total': total,
                                              'ok': ok, 'fail': fail})
            except Exception:  # noqa: BLE001
                pass

    cross = evaluate_pool(strategy_json, codes, start=start, end=end,
                          entry_timing=entry_timing, exit_timing=exit_timing,
                          workers=workers, progress_cb=_pc)
    bars = [r.bars for r in cross['results'] if r.bars]
    caveats = build_caveats(
        total=len(codes), scored=cross['overall'].get('scored', 0), with_industry=0,
        period_days=max(1, (date.fromisoformat(end) - date.fromisoformat(start)).days),
        bar_median=int(statistics.median(bars)) if bars else None,
        neutral_rate_median=cross['overall'].get('neutral_rate_median'))
    out = {
        'mode': mode,
        'pool': {'spec': pool_spec, 'codes': codes, 'n': len(codes),
                 'label': _scope_label(pool_spec),
                 'assets': {'stock': sum(1 for c in codes if _auto_detect_asset_type(c) != 'etf'),
                            'etf': sum(1 for c in codes if _auto_detect_asset_type(c) == 'etf')}},
        'period': [start, end], 'timing': {'entry': entry_timing, 'exit': exit_timing},
        'cross_section': {
            'basis': 'per_symbol_independent',
            'overall': cross['overall'], 'verdict': cross['verdict'],
            'caveats': caveats, 'errors': cross['errors'],
            'results': [asdict(r) for r in cross['results']],
        },
    }
    if with_portfolio:
        if progress_cb:
            try:
                progress_cb('pool_phase', {'phase': 'portfolio'})
            except Exception:  # noqa: BLE001
                pass
        pf = simulate_portfolio(
            cross['trades_by_code'], start=start, end=end,
            initial_cash=initial_cash, max_positions=max_positions)
        pf['basis'] = 'shared_cash'
        out['portfolio'] = pf
    return out
