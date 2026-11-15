"""factor_eval 单测。

核心手法：把因子**构造成「未来的收益本身」**，让 IC 有确定答案（=1），
再验证 lag / 分层 / 卫生层是否按设计生效。这比拿真实数据看「IC 看起来像不像」
强得多——后者无法区分「实现对」和「碰巧对」。
"""
import numpy as np
import pandas as pd
import pytest

from backtest.factor_eval import (
    EvalConfig,
    add_forward_returns,
    apply_hygiene,
    build_panel,
    compute_ic_series,
    compute_quantile_equity,
    compute_quantile_returns,
    compute_quantile_stats,
    compute_turnover,
    evaluate_factor,
    evaluate_factors,
    neutralize_factor,
    summarize_ic,
)


# ------------------------------------------------------------ 构造器

def _wide(n_dates: int = 80, n_codes: int = 40, seed: int = 3,
          drift: float = 0.0) -> pd.DataFrame:
    """宽表：index=date，columns=code，值=close。"""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.02, (n_dates, n_codes))
    prices = 10.0 * np.cumprod(1 + rets, axis=0)
    dates = pd.bdate_range("2024-01-02", periods=n_dates).strftime("%Y-%m-%d")
    cols = [f"{i:06d}" for i in range(1, n_codes + 1)]
    return pd.DataFrame(prices, index=dates, columns=cols)


def _panel_with_perfect_factor(n_dates: int = 80, n_codes: int = 40,
                               h: int = 5, seed: int = 3) -> pd.DataFrame:
    """面板，其中 f_ret_5 == 该日的前向收益（无滞后）→ 同日 IC 应 = 1。

    返回的面板已带 ``fwd_ret`` 列（与 f_ret_5 逐位相同），可直接喂评估函数。
    """
    wide = _wide(n_dates, n_codes, seed)
    fwd = wide.shift(-h) / wide - 1.0

    frames = []
    for code in wide.columns:
        s = pd.DataFrame({
            "trade_date": wide.index,
            "code": code,
            "close": wide[code].to_numpy(),
            "volume": 1_000_000.0,
        })
        s["f_ret_5"] = fwd[code].to_numpy()
        frames.append(s)
    panel = pd.concat(frames, ignore_index=True)
    return add_forward_returns(panel, h)


# ------------------------------------------------------------ 前向收益

def test_forward_returns_exact():
    panel = pd.DataFrame({
        "code": ["A"] * 4,
        "trade_date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
        "close": [100.0, 110.0, 121.0, 133.1],
    })
    out = add_forward_returns(panel, period=1)
    assert out["fwd_ret"].iloc[0] == pytest.approx(0.10)
    assert out["fwd_ret"].iloc[1] == pytest.approx(0.10)
    assert out["fwd_ret"].iloc[2] == pytest.approx(0.10)
    assert pd.isna(out["fwd_ret"].iloc[3]), "末 h 行必须为 NaN（不入统计）"


def test_forward_returns_no_cross_code_leak():
    """不同票之间不得串行计算（否则 A 的末行会拿到 B 的首行）。"""
    panel = pd.DataFrame({
        "code": ["A", "A", "B", "B"],
        "trade_date": ["2024-01-02", "2024-01-03", "2024-01-02", "2024-01-03"],
        "close": [10.0, 20.0, 100.0, 50.0],
    })
    out = add_forward_returns(panel, period=1).sort_values(["code", "trade_date"])
    a = out[out.code == "A"]["fwd_ret"].tolist()
    b = out[out.code == "B"]["fwd_ret"].tolist()
    assert a[0] == pytest.approx(1.0) and pd.isna(a[1])
    assert b[0] == pytest.approx(-0.5) and pd.isna(b[1])


# ------------------------------------------------------------ IC（核心）

def test_ic_perfect_positive_and_negative():
    panel = _panel_with_perfect_factor()
    ic = compute_ic_series(panel, "f_ret_5", "fwd_ret", "spearman", min_names=10)
    assert len(ic) > 50
    assert ic.mean() == pytest.approx(1.0, abs=1e-9)

    panel_neg = panel.copy()
    panel_neg["f_neg"] = -panel_neg["f_ret_5"]
    ic_neg = compute_ic_series(panel_neg, "f_neg", "fwd_ret", "spearman", min_names=10)
    assert ic_neg.mean() == pytest.approx(-1.0, abs=1e-9)


def test_lag_kills_lookahead():
    """完美因子（= 同日未来收益）滞后一天后，IC 必须塌到 0 附近。

    用 **1 日**收益构造：f[t] = r[t+1]（明天的收益）。
    - lag=0 → IC = 1
    - lag=1 → f[t−1] = r[t]（今天的收益）对 r[t+1]，随机游走下不相关 → IC ≈ 0

    ⚠️ 别用 5 日收益做这个断言：5 日收益平移 1 天后窗口重叠 4/5，
    相关系数天然 ≈ 0.8，会误判成「有前视」。见下一个用例。
    """
    panel = _panel_with_perfect_factor(h=1)
    cfg0 = EvalConfig(ret_period=1, lag=0, min_names=10)
    cfg1 = EvalConfig(ret_period=1, lag=1, min_names=10)

    r0 = evaluate_factor(panel, "ret_5", cfg0)
    r1 = evaluate_factor(panel, "ret_5", cfg1)

    assert r0["kpi"]["ic_mean"] == pytest.approx(1.0, abs=1e-6)
    assert abs(r1["kpi"]["ic_mean"]) < 0.15, "lag=1 后仍高度相关 → 存在前视"


def test_overlapping_window_correlation_is_not_lookahead():
    """5 日收益平移 1 天 → 理论相关 4/5 = 0.8（重叠窗口的数学必然）。

    这条钉住「lag 确实生效了」：若漏掉 shift，IC 会是 1.0 而非 0.8。
    """
    panel = _panel_with_perfect_factor(h=5)
    r = evaluate_factor(panel, "ret_5", EvalConfig(ret_period=5, lag=1, min_names=10))
    ic = r["kpi"]["ic_mean"]
    assert 0.6 < ic < 0.95, f"重叠窗口 IC 应 ≈0.8，实测 {ic}"
    assert ic < 0.999, "IC 仍为 1 → shift 未生效（前视）"


def test_ic_min_names_gate():
    panel = _panel_with_perfect_factor(n_codes=20)
    ic = compute_ic_series(panel, "f_ret_5", "fwd_ret", "spearman", min_names=30)
    assert len(ic) == 0, "截面不足 min_names 时应返回空（不出噪声值）"


def test_summarize_ic_handles_small_sample():
    s = summarize_ic(pd.Series([0.1, 0.2]))
    assert s["ic_mean"] is None and s["n_periods"] == 2


def test_summarize_ic_values():
    ic = pd.Series([0.1, 0.2, 0.3, -0.1])
    s = summarize_ic(ic)
    assert s["n_periods"] == 4
    assert s["ic_mean"] == pytest.approx(0.125)
    assert s["win_rate"] == pytest.approx(0.75)
    assert s["icir"] == pytest.approx(0.125 / ic.std(ddof=1))
    assert s["t_value"] == pytest.approx(s["icir"] * 2.0)


# ------------------------------------------------------------ 分层

def test_quantile_ordering_is_ascending():
    """Q1 = 因子值最小，Q5 = 最大；收益应随分层单调上行。"""
    panel = _panel_with_perfect_factor()
    qret = compute_quantile_returns(panel, "f_ret_5", "fwd_ret", quantiles=5, min_names=10)
    means = qret.mean()
    assert list(qret.columns) == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert means["Q1"] < means["Q2"] < means["Q3"] < means["Q4"] < means["Q5"]


def test_quantile_equity_and_long_short():
    panel = _panel_with_perfect_factor()
    qret = compute_quantile_returns(panel, "f_ret_5", "fwd_ret", 5, 10)
    eq = compute_quantile_equity(qret, 5)
    assert len(eq["dates"]) == len(qret)
    assert len(eq["series"]) == 5
    assert all(len(s["data"]) == len(eq["dates"]) for s in eq["series"])
    assert eq["long_short"]["name"] == "Q5−Q1 多空"
    assert eq["benchmark"]["name"] == "等权基准"
    # 完美因子 → 多空净值必须显著上行
    assert eq["long_short"]["data"][-1] > eq["long_short"]["data"][0]


def test_long_short_survives_non_default_quantiles():
    """分层 ≠ 5 时多空必须是「最高分位 − Q1」，不能因硬编码 Q5 而整列失效。

    真实事故（2026-10-17 用户在面板选「分层 3」）：多空夏普全是「—」，
    连带 DSR 恒为「未通过」、N 恒为「—」，因为 _ls_returns 只认 Q5/Q1。
    """
    panel = _panel_with_perfect_factor()
    for q in (3, 10):
        r = evaluate_factor(panel, "ret_5",
                            EvalConfig(ret_period=5, lag=0, quantiles=q, min_names=10))
        eq = r["quantile_equity"]
        assert eq["long_short"] is not None, f"quantiles={q} 时多空缺失"
        assert eq["long_short"]["name"] == f"Q{q}−Q1 多空"
        assert r["kpi"]["long_short_sharpe"] is not None, f"quantiles={q} 时多空夏普缺失"
        assert r["kpi"]["long_short_annual"] is not None

    # 「多空年化」必须来自价差序列，不能拿最高分位那一层充数
    r3 = evaluate_factor(panel, "ret_5",
                         EvalConfig(ret_period=5, lag=0, quantiles=3, min_names=10))
    qret = compute_quantile_returns(panel, "f_ret_5", "fwd_ret", 3, 10)
    spread = qret["Q3"] - qret["Q1"]
    expected = compute_quantile_stats(pd.DataFrame({"LS": spread}), 5)["LS"]["annual_return"]
    assert r3["kpi"]["long_short_annual"] == pytest.approx(expected)
    # 完美因子：价差年化与价差夏普必须同号（旧实现会出现一正一负）
    assert (r3["kpi"]["long_short_annual"] > 0) == (r3["kpi"]["long_short_sharpe"] > 0)

    # 批量路径下 n_trials 也应恢复（依赖各因子多空夏普存在）
    # 合成面板只造了 f_ret_5，这里复制一列当第二个因子，验证批量链路本身
    panel2 = panel.copy()
    panel2["f_ret_20"] = panel2["f_ret_5"]
    out = evaluate_factors(panel2, ["ret_5", "ret_20"],
                           EvalConfig(ret_period=5, lag=0, quantiles=3, min_names=10))
    assert out["significance"]["n_trials"] == 2


def test_quantile_stats_shapes():
    panel = _panel_with_perfect_factor()
    qret = compute_quantile_returns(panel, "f_ret_5", "fwd_ret", 5, 10)
    stats = compute_quantile_stats(qret, ret_period=5)
    assert set(stats) == {"Q1", "Q2", "Q3", "Q4", "Q5"}
    for v in stats.values():
        assert set(v) >= {"total_return", "annual_return", "sharpe",
                          "max_drawdown", "win_rate", "n_periods"}
    assert stats["Q5"]["total_return"] > stats["Q1"]["total_return"]


def test_turnover_bounded():
    panel = _panel_with_perfect_factor()
    to = compute_turnover(panel, "f_ret_5", 5, 10)
    assert to["series"] and len(to["dates"]) == len(to["series"][0]["data"])
    for s in to["series"]:
        for v in s["data"]:
            if v is not None:
                assert 0.0 <= v <= 1.0 + 1e-9


# ------------------------------------------------------------ 卫生层

def test_hygiene_drops_suspended_and_limit_days():
    panel = pd.DataFrame({
        "trade_date": ["2024-01-02"] * 4,
        "code": ["A", "B", "C", "D"],
        "close": [10.0] * 4,
        "volume": [1000.0, 0.0, 1000.0, 1000.0],
        "pct_change": [1.0, 2.0, 10.0, -9.8],
        "f_x": [1.0, 2.0, 3.0, 4.0],
    })
    cfg = EvalConfig(exclude_suspended=True, exclude_limit=True,
                     limit_pct=0.10, limit_tol=0.005)
    out = apply_hygiene(panel, cfg)
    kept = set(out["code"])
    assert "B" not in kept, "停牌（volume=0）应剔除"
    assert "C" not in kept, "涨停（10%）应剔除"
    assert "D" not in kept, "跌停（-9.8%）应剔除"
    assert "A" in kept


def test_hygiene_off_keeps_all():
    panel = pd.DataFrame({
        "trade_date": ["2024-01-02"] * 2,
        "code": ["A", "B"], "close": [10.0, 10.0],
        "volume": [0.0, 1000.0], "pct_change": [10.0, 1.0],
        "f_x": [1.0, 2.0],
    })
    cfg = EvalConfig(exclude_suspended=False, exclude_limit=False)
    assert len(apply_hygiene(panel, cfg)) == 2


# ------------------------------------------------------------ 面板与批量

def _ohlc_frame(code: str, n: int = 200, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 15.0 * np.cumprod(1 + rng.normal(0.0003, 0.02, n))
    return pd.DataFrame({
        "trade_date": pd.bdate_range("2024-01-02", periods=n).strftime("%Y-%m-%d"),
        "open": close * 0.999, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": rng.uniform(1e6, 3e6, n),
        "amount": close * 1e6, "pct_change": 0.0,
        "_code": code,
    })


def test_build_panel_has_factor_columns():
    frames = {f"60000{i}": _ohlc_frame(f"60000{i}", seed=i) for i in range(1, 4)}
    panel = build_panel(frames, ["ma_bias_20", "rsi_14"], min_history=60)
    assert not panel.empty
    assert {"trade_date", "code", "close", "volume", "pct_change"} <= set(panel.columns)
    assert {"f_ma_bias_20", "f_rsi_14"} <= set(panel.columns)
    assert panel["code"].nunique() == 3


def test_build_panel_skips_short_history():
    frames = {"600001": _ohlc_frame("600001", n=200),
              "600002": _ohlc_frame("600002", n=10)}
    panel = build_panel(frames, ["rsi_14"], min_history=60)
    assert panel["code"].nunique() == 1


def test_evaluate_factors_dsr_uses_batch_trials():
    """DSR 的 n_trials 必须等于本批因子数（多重检验校正的真实来源）。"""
    frames = {f"60000{i}": _ohlc_frame(f"60000{i}", n=200, seed=i) for i in range(1, 7)}
    panel = build_panel(frames, ["rsi_14", "macd_dif", "boll_pctb", "ret_20"],
                        min_history=60)
    cfg = EvalConfig(ret_period=5, min_names=5, lag=1)
    out = evaluate_factors(panel, ["rsi_14", "macd_dif", "boll_pctb", "ret_20"], cfg,
                           meta={"data_range": "test", "adjust": "qfq"})

    assert out["significance"]["n_trials"] == 4
    assert len(out["ranking"]) == 4
    assert out["errors"] == []
    # 排序按 |RankIC| 降序（Spearman 为主口径）
    ics = [abs(r["rank_ic_mean"]) for r in out["ranking"] if r["rank_ic_mean"] is not None]
    assert ics == sorted(ics, reverse=True)
    # 每因子都带参数回显与六项口径摘要
    assert out["params"]["ret_period"] == 5
    summary = out["appendix"]["summary"]
    for k in ("data_range", "ret_period", "rebalance", "adjust",
              "neutralize", "cost"):
        assert summary[k] not in (None, ""), f"口径摘要缺 {k}"


def test_evaluate_factor_error_is_isolated():
    frames = {"600001": _ohlc_frame("600001", n=200)}
    panel = build_panel(frames, ["rsi_14"], min_history=60)
    out = evaluate_factors(panel, ["rsi_14", "no_such_factor"],
                           EvalConfig(min_names=5, lag=1))
    assert len(out["errors"]) == 1
    assert out["errors"][0]["factor_id"] == "no_such_factor"
    assert "rsi_14" in out["factors"]


# ------------------------------------------------------------ 进度回调（SSE 配套）

def test_progress_callback_reports_each_factor():
    """F7：SSE 的进度条靠这个回调——每个因子必须各报一次，序号连续。"""
    frames = {f"60000{i}": _ohlc_frame(f"60000{i}", n=200, seed=i) for i in range(1, 5)}
    panel = build_panel(frames, ["rsi_14", "macd_dif"], min_history=60)
    events = []
    evaluate_factors(panel, ["rsi_14", "macd_dif"],
                     EvalConfig(min_names=5, lag=1),
                     progress=lambda ph, i, t, l: events.append((ph, i, t, l)))
    ev = [e for e in events if e[0] == "evaluate"]
    assert len(ev) == 2
    assert [e[1] for e in ev] == [1, 2], "序号必须连续"
    assert ev[-1][2] == 2 and ev[-1][3] == "MACD-DIF"


def test_progress_cancel_stops_batch():
    """取消语义：progress 抛 FactorRunCancelled → 整批**向上抛**（由 worker 捕获
    标记 cancelled），不能被「单因子失败隔离」逻辑吞掉。"""
    from backtest.factor_eval import FactorRunCancelled
    import pytest as _pytest

    frames = {f"60000{i}": _ohlc_frame(f"60000{i}", n=200, seed=i) for i in range(1, 4)}
    panel = build_panel(frames, ["rsi_14", "macd_dif"], min_history=60)
    seen = []
    with _pytest.raises(FactorRunCancelled):
        evaluate_factors(panel, ["rsi_14", "macd_dif"],
                         EvalConfig(min_names=5, lag=1),
                         progress=lambda ph, i, t, l: seen.append(i) or (
                             (_ for _ in ()).throw(FactorRunCancelled("取消"))
                             if i == 1 else None))
    assert seen == [1], "取消应发生在第一个因子边界"


def test_progress_exception_does_not_break_evaluation():
    """进度回调自身抛普通异常 → 吞掉继续算（可观测性不能打断业务）。"""
    frames = {f"60000{i}": _ohlc_frame(f"60000{i}", n=200, seed=i) for i in range(1, 4)}
    panel = build_panel(frames, ["rsi_14"], min_history=60)
    out = evaluate_factors(panel, ["rsi_14"], EvalConfig(min_names=5, lag=1),
                           progress=lambda *a: 1 / 0)
    assert out["coverage"]["evaluated"] == 1


# ------------------------------------------------------------ 中性化

def _contaminated_panel(n_codes: int = 40, n_dates: int = 60,
                        n_ind: int = 4) -> tuple[pd.DataFrame, np.ndarray]:
    """构造「纯信号 + 行业效应 + 市值效应」的因子，未来收益只由纯信号决定。

    因子 = α_i + 3·行业效应 + 2·log(size)
    前向收益 = α_i
    → 未中性化时 IC 被行业/市值污染；中性化后残差应恢复 α_i（IC→1）。
    """
    rng = np.random.default_rng(21)
    alpha = rng.normal(0, 0.05, n_codes)
    ind_effect = rng.normal(0, 1.0, n_ind)
    log_size = rng.normal(10, 1.0, n_codes)
    ind_idx = np.repeat(np.arange(n_ind), n_codes // n_ind)
    industry = np.array([f"IND{i}" for i in ind_idx])

    dates = pd.bdate_range("2025-01-02", periods=n_dates).strftime("%Y-%m-%d")
    rows = []
    for i in range(n_codes):
        f_val = alpha[i] + 3.0 * ind_effect[ind_idx[i]] + 2.0 * log_size[i]
        for j, d in enumerate(dates):
            rows.append({
                "trade_date": d,
                "code": f"{600000 + i}",
                "close": 10.0,
                "volume": 1e6,
                "pct_change": 0.0,
                "f_ret_5": f_val,
                "fwd_ret": alpha[i],       # 未来收益只由纯信号决定
                "industry": industry[i],
                "size": float(np.exp(log_size[i])),
            })
    return pd.DataFrame(rows), alpha


def test_neutralize_none_returns_original():
    panel, _ = _contaminated_panel()
    original = panel["f_ret_5"].copy()
    out = neutralize_factor(panel, "f_ret_5", EvalConfig(neutralize="none"))
    assert out.equals(original)


def test_neutralize_recovers_pure_signal():
    """构造：因子 = α + 3·行业 + 2·log(size)，未来收益只由 α 决定。

    实测（2026-10-17）：
        原始 IC = -0.034（被污染到符号都反了）
        仅行业  = -0.052（市值污染仍在）
        仅市值  = +0.172（行业污染仍在）
        行业+市值 = +0.974（恢复纯信号）
    ⚠️ 残差不可能恰好等于 α：α 自身与行业/市值有随机相关，回归会连带投影掉
    一部分——所以断言是「接近 1 且远好于原值」，不是「等于 1」。
    """
    panel, _ = _contaminated_panel()

    raw_ic = compute_ic_series(panel, "f_ret_5", "fwd_ret", "spearman", min_names=10)
    residual = neutralize_factor(panel, "f_ret_5", EvalConfig(neutralize="both"))
    panel2 = panel.copy()
    panel2["f_neut"] = residual
    neut_ic = compute_ic_series(panel2, "f_neut", "fwd_ret", "spearman", min_names=10)

    assert abs(raw_ic.mean()) < 0.2, f"未中性化的因子本应被污染，实测 IC={raw_ic.mean():.3f}"
    assert neut_ic.mean() > 0.95, f"中性化后应恢复纯信号，实测 {neut_ic.mean():.3f}"
    assert neut_ic.mean() - raw_ic.mean() > 0.5, "中性化改善幅度不足"


def test_neutralize_industry_only_and_size_only():
    panel, _ = _contaminated_panel()
    for mode in ("industry", "size"):
        r = neutralize_factor(panel, "f_ret_5", EvalConfig(neutralize=mode))
        assert r.notna().sum() > 0
        # 单维中性化只能去掉一半污染，IC 不应达到 1
        p = panel.copy()
        p["f_n"] = r
        ic = compute_ic_series(p, "f_n", "fwd_ret", "spearman", min_names=10)
        assert abs(ic.mean()) < 1.0


def test_neutralize_missing_industry_falls_into_unknown_group():
    panel, _ = _contaminated_panel()
    panel.loc[panel["code"] == "600000", "industry"] = None
    r, diag = neutralize_factor(panel, "f_ret_5",
                                EvalConfig(neutralize="industry"),
                                return_diagnostics=True)
    assert diag["mode"] == "industry"
    assert diag["industry_groups"] >= 2
    assert r.notna().sum() > 0, "行业缺失不应导致整列 NaN"


def test_neutralize_small_cross_section_keeps_original():
    """单日样本 < 10 时不做回归（不造残差）。"""
    panel = pd.DataFrame({
        "trade_date": ["2025-01-02"] * 5,
        "code": [f"60000{i}" for i in range(5)],
        "f_x": [1.0, 2.0, 3.0, 4.0, 5.0],
        "industry": ["A", "A", "B", "B", "B"],
        "size": [1.0, 2.0, 3.0, 4.0, 5.0],
    })
    r = neutralize_factor(panel, "f_x", EvalConfig(neutralize="both"))
    assert r.tolist() == panel["f_x"].tolist()


def test_evaluate_factor_reports_neutralization_diag():
    panel, _ = _contaminated_panel()
    r = evaluate_factor(panel, "ret_5", EvalConfig(ret_period=5, lag=1, min_names=10,
                                                   neutralize="industry"))
    assert r["neutralization"]["mode"] == "industry"
    assert r["neutralization"]["dates_regressed"] > 0
