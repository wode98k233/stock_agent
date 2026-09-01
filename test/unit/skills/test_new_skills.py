"""
Test: 新增 Skill 及风险指标计算
验证: 5 个新 skill 自动注册、工具构建、risk_calc 纯逻辑计算、fetcher 方法存在
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import numpy as np
import pandas as pd


# ── Skill 自动发现 ─────────────────────────────────────────

def test_skill_discovery():
    print("=" * 60)
    print("TC-NewSkill-01: 5 个新 Skill 自动注册")
    print("=" * 60)

    from tools.skill_register import SkillRegister
    sr = SkillRegister()
    sr.auto_discover()

    new_skills = ['money_flow']
    all_skills = sr.get_all_skills_unchecked()  # 含禁用技能，验证注册而非启用

    for name in new_skills:
        assert name in all_skills, f"Skill '{name}' 未注册"
        print(f"  [OK] {name} 已注册")

    print(f"[OK] 全部 {len(new_skills)} 个新 Skill 已注册 (总 {len(all_skills)} 个)")


def test_skill_tool_building():
    print("\n" + "=" * 60)
    print("TC-NewSkill-02: 新 Skill 工具构建")
    print("=" * 60)

    import logging
    logger = logging.getLogger('test')

    from tools.skill_register import SkillRegister
    sr = SkillRegister()
    sr.auto_discover()

    expected = {
        'money_flow': ['get_stock_fund_flow', 'get_sector_fund_flow', 'get_north_fund_flow'],
    }

    for skill_name, expected_tools in expected.items():
        # get_skill_unchecked 不检查 enabled 状态，避免被配置文件干扰
        meta = sr.get_skill_unchecked(skill_name)
        assert meta is not None, f"Skill '{skill_name}' 不存在"
        assert meta.build_tools_func is not None, f"Skill '{skill_name}' 无 build_tools_func"

        tools = meta.build_tools_func(logger, None)
        tool_names = [t.name for t in tools]

        for et in expected_tools:
            assert et in tool_names, f"{skill_name}: 缺少工具 '{et}'，实际: {tool_names}"

        print(f"  [OK] {skill_name}: {len(tools)} 个工具 {tool_names}")

    print(f"[OK] 全部工具构建成功")


# ── Fetcher 方法 ──────────────────────────────────────────

def test_fetcher_methods_exist():
    print("\n" + "=" * 60)
    print("TC-NewSkill-03: DataSource 新增方法存在")
    print("=" * 60)

    from tools.fetcher.base import DataSource

    new_methods = [
        'get_individual_fund_flow',
        'get_sector_fund_flow_rank',
        'get_north_fund_flow',
        'get_margin_trading',
        'get_margin_detail',
        'get_block_trades',
        'get_block_trade_stats',
        'get_board_industry_spot',
        'get_board_industry_hist',
    ]

    for method in new_methods:
        assert hasattr(DataSource, method), f"DataSource 缺少方法 '{method}'"
        print(f"  [OK] DataSource.{method}")

    print(f"[OK] 全部 {len(new_methods)} 个方法存在")


def test_fetcher_public_apis():
    print("\n" + "=" * 60)
    print("TC-NewSkill-04: fetcher 公开 API 存在")
    print("=" * 60)

    from tools import fetcher

    new_apis = [
        'ak_individual_fund_flow',
        'ak_sector_fund_flow_rank',
        'ak_north_fund_flow',
        'ak_margin_trading',
        'ak_margin_detail',
        'ak_block_trades',
        'ak_block_trade_stats',
        'ak_board_industry_spot',
        'ak_board_industry_hist',
    ]

    for api in new_apis:
        assert hasattr(fetcher, api), f"fetcher 缺少公开 API '{api}'"
        assert callable(getattr(fetcher, api)), f"'{api}' 不可调用"
        print(f"  [OK] {api}")

    print(f"[OK] 全部 {len(new_apis)} 个公开 API 存在且可调用")


def test_sector_rotation_ranking_timeout(monkeypatch, mock_logger):
    """板块轮动接口超时时应快速返回错误结构，而不是长期卡住。"""
    import importlib.util
    from pathlib import Path

    module_path = Path("tools/skills/sector_rotation/main.py")
    spec = importlib.util.spec_from_file_location("sector_rotation_main_for_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def timeout_runner(*args, **kwargs):
        raise TimeoutError("超过 1 秒")

    monkeypatch.setattr(module, "_run_with_timeout", timeout_runner)
    monkeypatch.setattr(
        "utils.cache.get_sector_rotation_cache",
        lambda key: None,
    )
    skill = module.SectorRotationSkill(mock_logger)
    payload = skill.get_sector_ranking("行业板块")

    assert "超时" in payload
    assert "板块实时行情" in payload


# ── risk_calc 纯逻辑测试 ──────────────────────────────────

def test_risk_calc_basic():
    print("\n" + "=" * 60)
    print("TC-NewSkill-05: risk_calc 基本计算")
    print("=" * 60)

    from tools.risk_calc import calc_risk_metrics

    # 构造 120 天上涨趋势
    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=120, freq='B')
    prices = 100 * np.cumprod(1 + np.random.normal(0.001, 0.015, 120))
    df = pd.DataFrame({'close': prices}, index=dates)

    m = calc_risk_metrics(df)

    assert 'volatility' in m, "缺少 volatility"
    assert 'max_drawdown' in m, "缺少 max_drawdown"
    assert 'sharpe_ratio' in m, "缺少 sharpe_ratio"
    assert 'sortino_ratio' in m, "缺少 sortino_ratio"
    assert 'var_95_daily' in m, "缺少 var_95_daily"
    assert 'calmar_ratio' in m, "缺少 calmar_ratio"
    assert 'risk_level' in m, "缺少 risk_level"
    assert m['trading_days'] == 119
    assert m['risk_free_rate'] == 2.5

    # 波动率应该在合理范围
    assert 0 < m['volatility'] < 100, f"波动率异常: {m['volatility']}"
    # 最大回撤应该在 0-100 之间
    assert 0 <= m['max_drawdown'] <= 100, f"最大回撤异常: {m['max_drawdown']}"
    # VaR 应该是负数
    assert m['var_95_daily'] < 0, f"VaR 应为负数: {m['var_95_daily']}"

    print(f"  波动率: {m['volatility']}%")
    print(f"  最大回撤: {m['max_drawdown']}%")
    print(f"  夏普比率: {m['sharpe_ratio']}")
    print(f"  Sortino: {m['sortino_ratio']}")
    print(f"  VaR 95%: {m['var_95_daily']}%")
    print(f"  风险等级: {m['risk_level']}")
    print("[OK] 基本计算正确")


def test_risk_calc_with_benchmark():
    print("\n" + "=" * 60)
    print("TC-NewSkill-06: risk_calc 含基准 Beta 计算")
    print("=" * 60)

    from tools.risk_calc import calc_risk_metrics

    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=120, freq='B')

    # 股票：高波动
    stock_prices = 100 * np.cumprod(1 + np.random.normal(0.001, 0.025, 120))
    stock_df = pd.DataFrame({'close': stock_prices}, index=dates)

    # 基准：低波动
    bench_prices = 100 * np.cumprod(1 + np.random.normal(0.0005, 0.012, 120))
    bench_df = pd.DataFrame({'close': bench_prices}, index=dates)

    m = calc_risk_metrics(stock_df, bench_df)

    assert 'beta' in m, "缺少 beta"
    assert 'alpha' in m, "缺少 alpha"
    assert 'correlation' in m, "缺少 correlation"

    # Beta 应该是有限数
    assert np.isfinite(m['beta']), f"Beta 异常: {m['beta']}"
    # 相关系数在 -1 到 1 之间
    assert -1 <= m['correlation'] <= 1, f"相关系数异常: {m['correlation']}"

    print(f"  Beta: {m['beta']}")
    print(f"  Alpha: {m['alpha']}%")
    print(f"  相关系数: {m['correlation']}")
    print("[OK] Beta 计算正确")


def test_risk_calc_insufficient_data():
    print("\n" + "=" * 60)
    print("TC-NewSkill-07: risk_calc 数据不足处理")
    print("=" * 60)

    from tools.risk_calc import calc_risk_metrics

    # 只有 5 天数据
    df = pd.DataFrame({'close': [100, 101, 99, 102, 100]})
    m = calc_risk_metrics(df)

    assert 'error' in m, f"应返回错误信息，实际: {m}"
    print(f"  返回: {m}")
    print("[OK] 数据不足时正确返回错误")


def test_risk_calc_empty_df():
    print("\n" + "=" * 60)
    print("TC-NewSkill-08: risk_calc 空数据处理")
    print("=" * 60)

    from tools.risk_calc import calc_risk_metrics

    df = pd.DataFrame({'close': []})
    m = calc_risk_metrics(df)

    assert 'error' in m, f"应返回错误信息，实际: {m}"
    print(f"  返回: {m}")
    print("[OK] 空数据时正确返回错误")


def test_risk_calc_risk_levels():
    print("\n" + "=" * 60)
    print("TC-NewSkill-09: risk_calc 风险等级评估")
    print("=" * 60)

    from tools.risk_calc import calc_risk_metrics

    np.random.seed(123)
    dates = pd.date_range('2024-01-01', periods=120, freq='B')

    # 低风险：低波动、正收益
    low_vol = 100 * np.cumprod(1 + np.random.normal(0.0008, 0.005, 120))
    m_low = calc_risk_metrics(pd.DataFrame({'close': low_vol}, index=dates))

    # 高风险：高波动、负收益
    high_vol = 100 * np.cumprod(1 + np.random.normal(-0.002, 0.035, 120))
    m_high = calc_risk_metrics(pd.DataFrame({'close': high_vol}, index=dates))

    print(f"  低波动组: 波动率={m_low['volatility']}%, 回撤={m_low['max_drawdown']}%, 夏普={m_low['sharpe_ratio']}, 风险={m_low['risk_level']}")
    print(f"  高波动组: 波动率={m_high['volatility']}%, 回撤={m_high['max_drawdown']}%, 夏普={m_high['sharpe_ratio']}, 风险={m_high['risk_level']}")

    valid_levels = ('低风险', '中低风险', '中高风险', '高风险')
    assert m_low['risk_level'] in valid_levels
    assert m_high['risk_level'] in valid_levels
    level_order = {'低风险': 0, '中低风险': 1, '中高风险': 2, '高风险': 3}
    assert level_order[m_low['risk_level']] <= level_order[m_high['risk_level']], \
        f"低波动组风险({m_low['risk_level']})不应高于高波动组({m_high['risk_level']})"

    print("[OK] 风险等级评估合理")


# ── aggregator 新增字段 ────────────────────────────────────

def test_aggregator_overview_fields():
    print("\n" + "=" * 60)
    print("TC-NewSkill-10: aggregator stocks_overview 新增字段")
    print("=" * 60)

    from tools.aggregator import stocks_overview

    indicators = [
        {'code': '001', 'name': 'A', 'price': 10, 'change_pct': 1.5, 'rsi': 55, 'volume_ratio': 1.2},
        {'code': '002', 'name': 'B', 'price': 20, 'change_pct': -0.8, 'rsi': 42, 'volume_ratio': 0.9},
        {'code': '003', 'name': 'C', 'price': 30, 'change_pct': 2.1, 'rsi': 68, 'volume_ratio': 2.1},
    ]

    stats = stocks_overview(indicators)

    # 检查新增字段
    assert 'rsi_median' in stats, "缺少 rsi_median"
    assert 'rsi_mean' in stats, "缺少 rsi_mean"
    assert 'volume_ratio_mean' in stats, "缺少 volume_ratio_mean"
    assert 'price_change_mean' in stats, "缺少 price_change_mean"
    assert 'rising_count' in stats, "缺少 rising_count"
    assert 'falling_count' in stats, "缺少 falling_count"

    assert stats['rising_count'] == 2
    assert stats['falling_count'] == 1
    assert stats['rsi_median'] == 55.0  # 中位数

    print(f"  rsi_median={stats['rsi_median']}, rsi_mean={stats['rsi_mean']}")
    print(f"  rising={stats['rising_count']}, falling={stats['falling_count']}")
    print(f"  volume_ratio_mean={stats['volume_ratio_mean']}")
    print("[OK] stocks_overview 新增字段正确")


def test_aggregator_overview_empty():
    print("\n" + "=" * 60)
    print("TC-NewSkill-11: aggregator stocks_overview 空数据")
    print("=" * 60)

    from tools.aggregator import stocks_overview

    stats = stocks_overview([])
    assert stats['total'] == 0
    print(f"  返回: {stats}")
    print("[OK] 空数据处理正确")


# ── sentiment 时间衰减 ─────────────────────────────────────

def test_sentiment_recency_weight():
    print("\n" + "=" * 60)
    print("TC-NewSkill-12: sentiment 时间衰减权重")
    print("=" * 60)

    from tools.sentiment import _calc_recency_weight
    from datetime import datetime

    now = datetime.now()

    # 今天
    w, desc = _calc_recency_weight(now.strftime('%Y-%m-%d %H:%M:%S'))
    assert w == 1.0, f"今天权重应为 1.0, 实际 {w}"
    print(f"  今天: 权重={w}, 描述={desc}")

    # 3天前
    from datetime import timedelta
    t3 = (now - timedelta(days=3)).strftime('%Y-%m-%d')
    w, desc = _calc_recency_weight(t3)
    assert w == 0.8, f"3天前权重应为 0.8, 实际 {w}"
    print(f"  3天前: 权重={w}, 描述={desc}")

    # 10天前
    t10 = (now - timedelta(days=10)).strftime('%Y-%m-%d')
    w, desc = _calc_recency_weight(t10)
    assert w == 0.3, f"10天前权重应为 0.3, 实际 {w}"
    print(f"  10天前: 权重={w}, 描述={desc}")

    # 30天前
    t30 = (now - timedelta(days=30)).strftime('%Y-%m-%d')
    w, desc = _calc_recency_weight(t30)
    assert w == 0.3, f"30天前权重应为 0.3, 实际 {w}"
    print(f"  30天前: 权重={w}, 描述={desc}")

    # 60天前
    t60 = (now - timedelta(days=60)).strftime('%Y-%m-%d')
    w, desc = _calc_recency_weight(t60)
    assert w == 0.1, f"60天前权重应为 0.1, 实际 {w}"
    print(f"  60天前: 权重={w}, 描述={desc}")

    # 空字符串
    w, desc = _calc_recency_weight('')
    assert w == 0.5, f"空字符串权重应为 0.5, 实际 {w}"
    print(f"  空字符串: 权重={w}, 描述={desc}")

    # 相对时间
    w, desc = _calc_recency_weight('3小时前')
    assert w == 1.0, f"3小时前权重应为 1.0, 实际 {w}"
    print(f"  '3小时前': 权重={w}, 描述={desc}")

    w, desc = _calc_recency_weight('5天前')
    assert w == 0.5, f"5天前权重应为 0.5, 实际 {w}"
    print(f"  '5天前': 权重={w}, 描述={desc}")

    print("[OK] 时间衰减权重计算正确")


# ── tech_indicators 新增字段 ────────────────────────────────

def test_tech_indicators_trend_fields():
    print("\n" + "=" * 60)
    print("TC-NewSkill-13: tech_indicators 趋势字段")
    print("=" * 60)

    from tools.tech_indicators import calc_indicators

    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=120, freq='B')
    close = 100 * np.cumprod(1 + np.random.normal(0.0005, 0.015, 120))
    high = close * (1 + np.abs(np.random.normal(0, 0.005, 120)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, 120)))
    volume = np.random.randint(1000000, 5000000, 120).astype(float)

    df = pd.DataFrame({
        'close': close, 'high': high, 'low': low, 'volume': volume
    }, index=dates)

    result = calc_indicators(df)

    # 检查新增趋势字段
    trend_fields = [
        'rsi_5d_ago', 'rsi_trend',
        'macd_bar_5d_ago', 'macd_bar_trend',
        'macd_cross_days', 'ma_cross_days',
        'k_5d_ago', 'k_trend',
    ]

    for field in trend_fields:
        assert field in result, f"缺少字段 '{field}'"
        print(f"  [OK] {field} = {result[field]}")

    # 趋势方向应该是上升或下降
    assert result['rsi_trend'] in ('上升', '下降'), f"rsi_trend 异常: {result['rsi_trend']}"
    assert result['macd_bar_trend'] in ('放大', '缩小'), f"macd_bar_trend 异常: {result['macd_bar_trend']}"
    assert result['k_trend'] in ('上升', '下降'), f"k_trend 异常: {result['k_trend']}"

    # 交叉天数应该是 -1 或正整数
    assert result['macd_cross_days'] >= -1, f"macd_cross_days 异常: {result['macd_cross_days']}"
    assert result['ma_cross_days'] >= -1, f"ma_cross_days 异常: {result['ma_cross_days']}"

    print("[OK] 趋势字段计算正确")


def test_cache_fund_flow():
    print("\n" + "=" * 60)
    print("TC-NewSkill-14: 资金流向缓存读写")
    print("=" * 60)
    from utils.cache import get_fund_flow_cache, set_fund_flow_cache
    set_fund_flow_cache('test_symbol', {'symbol': '600519', 'data': [1, 2, 3]})
    cached = get_fund_flow_cache('test_symbol')
    assert cached is not None, "缓存应存在"
    assert cached['symbol'] == '600519', f"缓存数据不匹配: {cached}"
    print(f"  [OK] 缓存读写正常: {cached}")


def test_cache_margin():
    print("\n" + "=" * 60)
    print("TC-NewSkill-15: 融资融券缓存读写")
    print("=" * 60)
    from utils.cache import get_margin_cache, set_margin_cache
    set_margin_cache('test_margin', {'market': 'sh', 'days': 30, 'data': [1, 2]})
    cached = get_margin_cache('test_margin')
    assert cached is not None, "缓存应存在"
    assert cached['market'] == 'sh', f"缓存数据不匹配: {cached}"
    print(f"  [OK] 缓存读写正常: {cached}")


def test_cache_block_trade():
    print("\n" + "=" * 60)
    print("TC-NewSkill-16: 大宗交易缓存读写")
    print("=" * 60)
    from utils.cache import get_block_trade_cache, set_block_trade_cache
    set_block_trade_cache('test_block', {'symbol': 'A股', 'records': [10, 20]})
    cached = get_block_trade_cache('test_block')
    assert cached is not None, "缓存应存在"
    assert cached['symbol'] == 'A股', f"缓存数据不匹配: {cached}"
    print(f"  [OK] 缓存读写正常: {cached}")


def test_cache_sector_rotation():
    print("\n" + "=" * 60)
    print("TC-NewSkill-17: 板块轮动缓存读写")
    print("=" * 60)
    from utils.cache import get_sector_rotation_cache, set_sector_rotation_cache
    set_sector_rotation_cache('test_sector', {'type': '行业板块', 'rank': [1, 2, 3]})
    cached = get_sector_rotation_cache('test_sector')
    assert cached is not None, "缓存应存在"
    assert cached['type'] == '行业板块', f"缓存数据不匹配: {cached}"
    print(f"  [OK] 缓存读写正常: {cached}")


def test_cache_risk_metrics():
    print("\n" + "=" * 60)
    print("TC-NewSkill-18: 风险指标缓存读写")
    print("=" * 60)
    from utils.cache import get_risk_metrics_cache, set_risk_metrics_cache
    set_risk_metrics_cache('test_risk', {'symbol': '600519', 'volatility': 25.3})
    cached = get_risk_metrics_cache('test_risk')
    assert cached is not None, "缓存应存在"
    assert cached['symbol'] == '600519', f"缓存数据不匹配: {cached}"
    print(f"  [OK] 缓存读写正常: {cached}")


def test_risk_calc_zero_close():
    print("\n" + "=" * 60)
    print("TC-NewSkill-20: risk_calc close=0 处理")
    print("=" * 60)
    from tools.risk_calc import calc_risk_metrics
    df = pd.DataFrame({'close': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]})
    m = calc_risk_metrics(df)
    assert 'error' not in m, f"不应报错: {m}"
    assert m['total_return'] == 0, f"total_return应为0: {m['total_return']}"
    print(f"  [OK] close=0 处理正常: total_return={m['total_return']}")


def test_risk_calc_nan_values():
    print("\n" + "=" * 60)
    print("TC-NewSkill-21: risk_calc NaN 值处理")
    print("=" * 60)
    from tools.risk_calc import calc_risk_metrics
    np.random.seed(42)
    prices = 100 * np.cumprod(1 + np.random.normal(0.001, 0.015, 120))
    prices[5] = np.nan
    prices[20] = np.nan
    df = pd.DataFrame({'close': prices})
    m = calc_risk_metrics(df)
    assert 'error' not in m, f"不应报错: {m}"
    assert np.isfinite(m['volatility']), f"波动率应为有限数: {m['volatility']}"
    print(f"  [OK] NaN 处理正常: volatility={m['volatility']}%")


def test_risk_calc_single_negative():
    print("\n" + "=" * 60)
    print("TC-NewSkill-22: risk_calc 仅1天负收益")
    print("=" * 60)
    from tools.risk_calc import calc_risk_metrics
    np.random.seed(42)
    prices = 100 * np.cumprod(1 + np.abs(np.random.normal(0.001, 0.005, 120)))
    df = pd.DataFrame({'close': prices})
    m = calc_risk_metrics(df)
    assert 'error' not in m, f"不应报错: {m}"
    assert np.isfinite(m['sortino_ratio']), f"Sortino应为有限数: {m['sortino_ratio']}"
    print(f"  [OK] 仅1天负收益处理正常: sortino={m['sortino_ratio']}")


def test_unwrap_method():
    print("\n" + "=" * 60)
    print("TC-NewSkill-23: _unwrap 方法测试")
    print("=" * 60)
    import logging
    import importlib.util
    logger = logging.getLogger('test')
    spec = importlib.util.spec_from_file_location(
        "money_flow_main",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                     "tools", "skills", "money_flow", "main.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    skill = mod.MoneyFlowSkill(logger, None)
    result = skill._unwrap('{"key": "value"}')
    assert result == {'key': 'value'}, f"JSON字符串解析失败: {result}"
    result = skill._unwrap({'key': 'value'})
    assert result == {'key': 'value'}, f"dict应原样返回: {result}"
    result = skill._unwrap('invalid json')
    assert 'raw' in result, f"无效JSON应返回raw: {result}"
    print(f"  [OK] _unwrap 方法正常")


# ── 运行全部 ───────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_skill_discovery,
        test_skill_tool_building,
        test_fetcher_methods_exist,
        test_fetcher_public_apis,
        test_risk_calc_basic,
        test_risk_calc_with_benchmark,
        test_risk_calc_insufficient_data,
        test_risk_calc_empty_df,
        test_risk_calc_risk_levels,
        test_aggregator_overview_fields,
        test_aggregator_overview_empty,
        test_sentiment_recency_weight,
        test_tech_indicators_trend_fields,
        test_cache_fund_flow,
        test_cache_margin,
        test_cache_block_trade,
        test_cache_sector_rotation,
        test_cache_risk_metrics,
        test_risk_calc_zero_close,
        test_risk_calc_nan_values,
        test_risk_calc_single_negative,
        test_unwrap_method,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"\n[FAIL] {t.__name__}: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    if failed == 0:
        print(f"[PASS] 全部 {passed} 个测试通过!")
    else:
        print(f"[RESULT] {passed} passed, {failed} failed out of {len(tests)}")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
