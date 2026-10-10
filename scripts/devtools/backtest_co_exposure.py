"""co_exposure「两腿同弱时整体降仓 vs 内部调权」对比回测。

存在理由（为什么需要跑这一轮）：
    组合层 `data/portfolio_map.json` 的 `co_exposure.shared_risks[cn_demand]` 断言
    「两腿共享中国内需暴露，同时走弱时正确动作是**整体降仓**，而非把权重从一条腿
    挪到另一条腿」。该断言当前 `confidence.level = "low"`、触发线 ±3 为**机制占位值**，
    **没有回测支撑**。本脚本就是补这个支撑（或推翻它）。

口径与限制（必须先读，否则结论会被误读）：
    1. **状态变量是代理，不是 thesis 总分。** 无法等值还原总分——`dividend_env` 的
       核心因子 premium 依赖 `idx_h30269_dy`（股息率）序列，本地仅有 30 期；
       宏观 cn10_y/us10_y/usdcny 仅 283 期（2025-08 起）。故这里用两腿**价量派生**
       的状态（趋势 / 位置）作「弱」的判据——与 thesis 的 crowding/位置因子同族，
       但**不等于**「thesis 总分 ≤ -3」。
    2. 两腿共同可用区间受 H30269 限制：**2024-08-28 起**（约 507 个交易日，~2 年）。
    3. 无前视：t 日状态只作用于 t+1 收益；交易成本按换手计（默认单边 0.05%）。
    4. 现金收益按 0 计（保守）。真实货币基金/短债会有正收益，故「整体降仓」的
       实际相对表现会略好于本脚本读数。

用法：
    python scripts/devtools/backtest_co_exposure.py            # 终端摘要
    python scripts/devtools/backtest_co_exposure.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

DEFENSE_CODE, DEFENSE_NAME = "H30269", "红利低波"
OFFENSE_CODE, OFFENSE_NAME = "HSTECH", "恒生科技"

TRADE_COST = 0.0005      # 单边 5bp（保守偏乐观：未计冲击成本与涨跌停）
ANN = 252


# ── 数据 ──────────────────────────────────────────────────────────────

def load_closes() -> tuple[list[str], list[float], list[float]]:
    """共同交易日对齐后的 (dates, 防守腿收盘, 进攻腿收盘)。"""
    from utils.cache.market_data_db import get_index_daily

    series = {}
    for code in (DEFENSE_CODE, OFFENSE_CODE):
        df = get_index_daily(code)
        if df is None or not len(df):
            raise SystemExit(f"缺少 {code} 日线，无法回测")
        series[code] = dict(zip([str(x)[:10] for x in df["trade_date"].tolist()],
                                [float(x) for x in df["close"].tolist()]))
    common = sorted(set(series[DEFENSE_CODE]) & set(series[OFFENSE_CODE]))
    if len(common) < 260:
        raise SystemExit(f"共同交易日仅 {len(common)} 天，不足以回测")
    return (common,
            [series[DEFENSE_CODE][d] for d in common],
            [series[OFFENSE_CODE][d] for d in common])


# ── 状态：两腿是否「同弱」 ─────────────────────────────────────────────

def weak_flags(closes: list[float], rule: str, lookback: int) -> list[bool]:
    """逐日「该腿弱势」标记。只用 ≤ t 的数据（无前视）。

    rule:
      trend_neg   —— 近 lookback 日动量为负（趋势不利）
      below_ma    —— 点位低于 lookback 日均线（位置不利）
      mom_and_ma  —— 两者同时成立（更严格）
    """
    out = [False] * len(closes)
    for i in range(len(closes)):
        if i < lookback:
            continue
        mom = closes[i] / closes[i - lookback] - 1
        window = closes[i - lookback + 1:i + 1]
        ma = sum(window) / len(window)
        below = closes[i] < ma
        if rule == "trend_neg":
            out[i] = mom < 0
        elif rule == "below_ma":
            out[i] = below
        elif rule == "mom_and_ma":
            out[i] = mom < 0 and below
        else:
            raise ValueError(rule)
    return out


# ── 策略 ──────────────────────────────────────────────────────────────

def run_strategy(d_ret: list[float], o_ret: list[float], state: list[bool],
                 kind: str, params: dict | None = None) -> dict:
    """按状态换仓，返回净值与指标。

    kind:
      base    —— 恒 50/50（对照）
      rotate  —— 内部调权：触发时把进攻腿权重切给防守腿（100/0）
      derisk  —— 整体降仓：触发时总仓位降到 `gross`（默认 0.5），其余现金
      always_defense / always_offense —— 恒定满仓单腿（检验是否只是资产选择）

    无前视：第 t 日的权重由**第 t-1 日收盘后的状态**决定，作用于第 t 日收益。
    `cash_yield` 为现金年化收益（默认 0，对「降仓」最不利，属保守设定）。
    """
    params = params or {}
    gross = float(params.get("gross", 0.5))
    cash_annual = float(params.get("cash_yield", 0.0))
    cash_daily = (1 + cash_annual) ** (1 / ANN) - 1
    n = len(d_ret)
    nav = [1.0]
    prev_w = None
    turnovers = 0.0
    exposure = []

    for t in range(n):
        trig = state[t - 1] if t > 0 else False      # 前一日状态
        if kind == "base":
            w = (0.5, 0.5)
        elif kind == "rotate":
            w = (1.0, 0.0) if trig else (0.5, 0.5)
        elif kind == "derisk":
            # 未触发 = 与基准同仓（50/50，总 100%）；触发 = 总仓位降到 gross
            w = (gross * 0.5, gross * 0.5) if trig else (0.5, 0.5)
        elif kind == "always_defense":
            # 【关键对照】恒定 100% 防守腿。若它也赢过 base/rotate，说明「调权」的
            # 超额来自「一直持有更强的资产」（资产选择），而不是「同弱时切换」（择时能力）。
            w = (1.0, 0.0)
        elif kind == "always_offense":
            w = (0.0, 1.0)
        else:
            raise ValueError(kind)

        cost = 0.0
        if prev_w is not None:
            to = sum(abs(a - b) for a, b in zip(w, prev_w))
            cost = to * TRADE_COST
            turnovers += to
        prev_w = w

        r = w[0] * d_ret[t] + w[1] * o_ret[t] + (1 - w[0] - w[1]) * cash_daily - cost
        nav.append(nav[-1] * (1 + r))
        exposure.append(w[0] + w[1])

    return {
        "nav": nav,
        "metrics": _metrics(nav, exposure),
        "turnover_oneway": round(turnovers / 2, 2),
    }


def _metrics(nav: list[float], exposure: list[float]) -> dict:
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
    n = len(rets)
    if n == 0:
        return {}
    total = nav[-1] - 1
    ann_ret = (nav[-1] ** (ANN / n) - 1) if nav[-1] > 0 else None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    ann_vol = (var ** 0.5) * (ANN ** 0.5)
    sharpe = (ann_ret / ann_vol) if (ann_vol and ann_ret is not None) else None
    peak, mdd = nav[0], 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {
        "total_return_pct": round(total * 100, 2),
        "annual_return_pct": round(ann_ret * 100, 2) if ann_ret is not None else None,
        "annual_vol_pct": round(ann_vol * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "calmar": round(ann_ret / abs(mdd), 2) if (mdd and ann_ret is not None) else None,
        "avg_exposure_pct": round(sum(exposure) / len(exposure) * 100, 1),
    }


# ── 事件研究：每次触发后 20/60 日的三策略差 ─────────────────────────────

def event_study(d_ret, o_ret, state, horizons=(20, 60)) -> dict:
    """触发日之后，三策略的持有期收益对比（只看触发样本，不重叠去重）。"""
    trig_idx = [i for i in range(1, len(d_ret)) if state[i - 1]]
    out = {}
    for h in horizons:
        rows = []
        for i in trig_idx:
            if i + h > len(d_ret):
                continue
            nv_base = nv_rot = nv_der = 1.0
            for k in range(i, i + h):
                nv_base *= (1 + 0.5 * d_ret[k] + 0.5 * o_ret[k])
                nv_rot *= (1 + 1.0 * d_ret[k] + 0.0 * o_ret[k])
                nv_der *= (1 + 0.25 * d_ret[k] + 0.25 * o_ret[k])
            rows.append((nv_base - 1, nv_rot - 1, nv_der - 1))
        if not rows:
            continue
        n = len(rows)
        avg = lambda j: round(sum(r[j] for r in rows) / n * 100, 2)      # noqa: E731
        win = lambda j: round(sum(1 for r in rows if r[j] > 0) / n * 100, 1)  # noqa: E731
        out[f"h{h}"] = {
            "n_events": n,
            "base_avg_pct": avg(0), "rotate_avg_pct": avg(1), "derisk_avg_pct": avg(2),
            "base_win_pct": win(0), "rotate_win_pct": win(1), "derisk_win_pct": win(2),
            "derisk_minus_rotate_pp": round(avg(2) - avg(1), 2),
            "derisk_minus_base_pp": round(avg(2) - avg(0), 2),
        }
    return out


def regime_corr(d_ret, o_ret, state, lookback: int = 60) -> dict:
    """对照：全期 vs「两腿同弱期」的日收益相关性 + 各自平均收益。

    这是 co_exposure 机制成立与否的**前提检验**：该规则假设两腿共享底层暴露，
    因此同弱时「内部调权只是把风险搬来搬去」（隐含：同弱期相关性显著上升、
    分散化失效）。若相关性并未上升，则前提不成立。
    """
    def _corr(idx):
        xs = [d_ret[i] for i in idx]
        ys = [o_ret[i] for i in idx]
        n = len(xs)
        if n < 20:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
        vx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
        vy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
        return round(cov / (vx * vy), 3) if vx and vy else None

    all_idx = list(range(1, len(d_ret)))
    trg_idx = [i for i in all_idx if state[i - 1]]
    def avg(idx, r):
        return round(sum(r[i] for i in idx) / len(idx) * 100, 3) if idx else None
    return {
        "corr_all": _corr(all_idx), "n_all": len(all_idx),
        "corr_trigger": _corr(trg_idx), "n_trigger": len(trg_idx),
        "defense_avg_daily_pct": avg(all_idx, d_ret),
        "defense_avg_trigger_pct": avg(trg_idx, d_ret),
        "offense_avg_daily_pct": avg(all_idx, o_ret),
        "offense_avg_trigger_pct": avg(trg_idx, o_ret),
    }


# ── 主流程 ────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="把结果写到 JSON")
    args = ap.parse_args()

    dates, dc, oc = load_closes()
    d_ret = [0.0] + [dc[i] / dc[i - 1] - 1 for i in range(1, len(dc))]
    o_ret = [0.0] + [oc[i] / oc[i - 1] - 1 for i in range(1, len(oc))]

    print("=" * 74)
    print("co_exposure 对比回测：两腿同弱时「整体降仓」vs「内部调权」")
    print("=" * 74)
    print(f"区间 {dates[0]} → {dates[-1]}（{len(dates)} 个共同交易日）")
    print(f"防守腿 {DEFENSE_NAME}({DEFENSE_CODE})   进攻腿 {OFFENSE_NAME}({OFFENSE_CODE})")
    print(f"交易成本 单边 {TRADE_COST*100:.2f}%    现金收益按 0 计（保守）")
    print()

    results = {"range": [dates[0], dates[-1]], "n_days": len(dates),
               "defense": DEFENSE_CODE, "offense": OFFENSE_CODE,
               "trade_cost": TRADE_COST, "rules": {}}

    for rule, lookback in (("trend_neg", 20), ("below_ma", 250), ("mom_and_ma", 60)):
        dw = weak_flags(dc, rule, lookback)
        ow = weak_flags(oc, rule, lookback)
        both = [a and b for a, b in zip(dw, ow)]
        n_trig = sum(both[1:])
        print("-" * 74)
        print(f"【判据 {rule} / lookback={lookback}】两腿同弱 {n_trig} 天 "
              f"（占可判期 {n_trig}/{len(dates)} = {n_trig/len(dates)*100:.1f}%）")
        if n_trig < 10:
            print("  触发样本过少（<10），结论不具统计意义，跳过。")
            continue

        base = run_strategy(d_ret, o_ret, both, "base")
        rot = run_strategy(d_ret, o_ret, both, "rotate")
        ad = run_strategy(d_ret, o_ret, both, "always_defense")
        ao = run_strategy(d_ret, o_ret, both, "always_offense")
        for gross in (0.7, 0.5, 0.3):
            der = run_strategy(d_ret, o_ret, both, "derisk", {"gross": gross})
            tag = f"derisk{int(gross*100)}"
            results["rules"].setdefault(f"{rule}_{lookback}", {})[tag] = der["metrics"]
        results["rules"][f"{rule}_{lookback}"]["base"] = base["metrics"]
        results["rules"][f"{rule}_{lookback}"]["rotate"] = rot["metrics"]
        results["rules"][f"{rule}_{lookback}"]["always_defense"] = ad["metrics"]
        results["rules"][f"{rule}_{lookback}"]["always_offense"] = ao["metrics"]
        results["rules"][f"{rule}_{lookback}"]["n_trigger"] = n_trig

        hdr = f"{'策略':<18}{'累计':>9}{'年化':>9}{'波动':>9}{'最大回撤':>10}{'夏普':>7}{'均仓位':>8}"
        print(hdr)

        def _line(label, m):
            print(f"{label:<18}{m['total_return_pct']:>8.1f}%{m['annual_return_pct']:>8.1f}%"
                  f"{m['annual_vol_pct']:>8.1f}%{m['max_drawdown_pct']:>9.1f}%"
                  f"{m['sharpe']:>7.2f}{m['avg_exposure_pct']:>7.0f}%")

        _line("基准 50/50", base["metrics"])
        _line("内部调权 100/0", rot["metrics"])
        for gross in (0.7, 0.5, 0.3):
            _line("整体降仓 " + str(int(gross * 100)) + "%",
                  run_strategy(d_ret, o_ret, both, "derisk", {"gross": gross})["metrics"])
        print("  ── 对照（恒定满仓单腿，检验是否只是「资产选择」）──")
        _line("恒 100% 防守腿", ad["metrics"])
        _line("恒 100% 进攻腿", ao["metrics"])
        print("  ── 鲁棒性：现金给 2% 年化后，降仓能否翻盘 ──")
        for gross in (0.7, 0.5, 0.3):
            m = run_strategy(d_ret, o_ret, both, "derisk",
                             {"gross": gross, "cash_yield": 0.02})["metrics"]
            print(f"{'降仓' + str(int(gross*100)) + '% + 现金2%':<18}"
                  f"{m['total_return_pct']:>8.1f}%{m['annual_return_pct']:>8.1f}%"
                  f"{m['annual_vol_pct']:>8.1f}%{m['max_drawdown_pct']:>9.1f}%{m['sharpe']:>7.2f}"
                  f"{m['avg_exposure_pct']:>7.0f}%")
            results["rules"][f"{rule}_{lookback}"][f"derisk{int(gross*100)}_cash2"] = m
        ev = event_study(d_ret, o_ret, both)
        results["rules"][f"{rule}_{lookback}"]["event"] = ev
        for k, v in ev.items():
            print(f"  触发后{k[1:]}日：基准 {v['base_avg_pct']:+.2f}% / 调权 {v['rotate_avg_pct']:+.2f}% / "
                  f"降仓 {v['derisk_avg_pct']:+.2f}%  → 降仓−调权 {v['derisk_minus_rotate_pp']:+.2f}pp"
                  f"（n={v['n_events']}）")

        rc = regime_corr(d_ret, o_ret, both)
        results["rules"][f"{rule}_{lookback}"]["regime_corr"] = rc
        print(f"  相关性：全期 {rc['corr_all']}（n={rc['n_all']}） → 同弱期 {rc['corr_trigger']}"
              f"（n={rc['n_trigger']}） = "
              + ("上升 %.3f" % (rc['corr_trigger'] - rc['corr_all'])
                 if (rc['corr_trigger'] is not None and rc['corr_all'] is not None) else "n/a"))
        print(f"  同弱期日均：防守腿 {rc['defense_avg_trigger_pct']:+.3f}% vs 全期 {rc['defense_avg_daily_pct']:+.3f}%"
              f"  | 进攻腿 {rc['offense_avg_trigger_pct']:+.3f}% vs 全期 {rc['offense_avg_daily_pct']:+.3f}%")
        print()

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"结果已写入 {args.json}")


if __name__ == "__main__":
    main()
