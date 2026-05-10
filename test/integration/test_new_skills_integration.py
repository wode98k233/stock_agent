"""
Test: 5个新Skill集成测试（真实API调用）
验证: money_flow, margin_trading, sector_rotation, risk_metrics, block_trades
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('test_integration')


def test_money_flow_stock_fund_flow():
    print("=" * 60)
    print("IT-NewSkill-01: money_flow - 个股资金流向")
    print("=" * 60)
    from tools.skills.money_flow.main import MoneyFlowSkill
    skill = MoneyFlowSkill(logger, None)
    result = skill.get_stock_fund_flow("600519")
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert 'error' not in result or result.get('error') is None, f"获取失败: {result}"
    assert 'symbol' in result, f"缺少 symbol 字段: {result}"
    print(f"  symbol: {result['symbol']}")
    print(f"  数据天数: {result.get('total_days', 'N/A')}")
    print(f"  [OK] 个股资金流向获取成功")


def test_money_flow_sector_fund_flow():
    print("\n" + "=" * 60)
    print("IT-NewSkill-02: money_flow - 板块资金流向")
    print("=" * 60)
    from tools.skills.money_flow.main import MoneyFlowSkill
    skill = MoneyFlowSkill(logger, None)
    result = skill.get_sector_fund_flow("今日", "行业资金流")
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert isinstance(result, list), f"应返回列表: {type(result)}"
    print(f"  板块数量: {len(result)}")
    if result:
        print(f"  第一条: {result[0]}")
    print(f"  [OK] 板块资金流向获取成功")


def test_money_flow_north_fund():
    print("\n" + "=" * 60)
    print("IT-NewSkill-03: money_flow - 北向资金")
    print("=" * 60)
    from tools.skills.money_flow.main import MoneyFlowSkill
    skill = MoneyFlowSkill(logger, None)
    result = skill.get_north_fund_flow("北向资金")
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert 'error' not in result or result.get('error') is None, f"获取失败: {result}"
    print(f"  symbol: {result.get('symbol', 'N/A')}")
    print(f"  数据天数: {result.get('total_days', 'N/A')}")
    print(f"  [OK] 北向资金获取成功")


def test_margin_trading_summary():
    print("\n" + "=" * 60)
    print("IT-NewSkill-04: margin_trading - 两融余额汇总")
    print("=" * 60)
    from tools.skills.margin_trading.main import MarginTradingSkill
    skill = MarginTradingSkill(logger, None)
    result = skill.get_margin_summary("sh", 10)
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert 'error' not in result or result.get('error') is None, f"获取失败: {result}"
    print(f"  market: {result.get('market', 'N/A')}")
    print(f"  数据天数: {result.get('days', 'N/A')}")
    print(f"  [OK] 两融余额汇总获取成功")


def test_margin_trading_detail():
    print("\n" + "=" * 60)
    print("IT-NewSkill-05: margin_trading - 两融个股明细")
    print("=" * 60)
    from tools.skills.margin_trading.main import MarginTradingSkill
    skill = MarginTradingSkill(logger, None)
    result = skill.get_margin_detail("sh")
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert isinstance(result, list), f"应返回列表: {type(result)}"
    print(f"  明细数量: {len(result)}")
    print(f"  [OK] 两融个股明细获取成功")


def test_sector_rotation_ranking():
    print("\n" + "=" * 60)
    print("IT-NewSkill-06: sector_rotation - 板块行情排名")
    print("=" * 60)
    from tools.skills.sector_rotation.main import SectorRotationSkill
    skill = SectorRotationSkill(logger, None)
    result = skill.get_sector_ranking("行业板块")
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert isinstance(result, list), f"应返回列表: {type(result)}"
    print(f"  板块数量: {len(result)}")
    if result:
        print(f"  第一条: {result[0]}")
    print(f"  [OK] 板块行情排名获取成功")


def test_sector_rotation_history():
    print("\n" + "=" * 60)
    print("IT-NewSkill-07: sector_rotation - 板块历史K线")
    print("=" * 60)
    from tools.skills.sector_rotation.main import SectorRotationSkill
    skill = SectorRotationSkill(logger, None)
    result = skill.get_sector_history("电力", 30)
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert 'error' not in result or result.get('error') is None, f"获取失败: {result}"
    print(f"  symbol: {result.get('symbol', 'N/A')}")
    print(f"  数据天数: {result.get('days', 'N/A')}")
    print(f"  [OK] 板块历史K线获取成功")


def test_sector_rotation_fund_flow():
    print("\n" + "=" * 60)
    print("IT-NewSkill-08: sector_rotation - 板块资金流向")
    print("=" * 60)
    from tools.skills.sector_rotation.main import SectorRotationSkill
    skill = SectorRotationSkill(logger, None)
    result = skill.get_sector_fund_flow("今日", "行业资金流")
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert isinstance(result, list), f"应返回列表: {type(result)}"
    print(f"  板块数量: {len(result)}")
    print(f"  [OK] 板块资金流向获取成功")


def test_risk_metrics():
    print("\n" + "=" * 60)
    print("IT-NewSkill-09: risk_metrics - 风险指标计算")
    print("=" * 60)
    from tools.skills.risk_metrics.main import RiskMetricsSkill
    skill = RiskMetricsSkill(logger, None)
    result = skill.get_risk_metrics("600519", 120, "000300")
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert 'error' not in result or result.get('error') is None, f"获取失败: {result}"
    print(f"  symbol: {result.get('symbol', 'N/A')}")
    print(f"  波动率: {result.get('volatility', 'N/A')}%")
    print(f"  最大回撤: {result.get('max_drawdown', 'N/A')}%")
    print(f"  夏普比率: {result.get('sharpe_ratio', 'N/A')}")
    print(f"  风险等级: {result.get('risk_level', 'N/A')}")
    if 'beta' in result:
        print(f"  Beta: {result['beta']}")
    print(f"  [OK] 风险指标计算成功")


def test_block_trades_detail():
    print("\n" + "=" * 60)
    print("IT-NewSkill-10: block_trades - 大宗交易明细")
    print("=" * 60)
    from tools.skills.block_trades.main import BlockTradesSkill
    skill = BlockTradesSkill(logger, None)
    result = skill.get_block_trade_detail("A股", 7)
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert isinstance(result, list), f"应返回列表: {type(result)}"
    print(f"  交易记录数: {len(result)}")
    print(f"  [OK] 大宗交易明细获取成功")


def test_block_trades_stats():
    print("\n" + "=" * 60)
    print("IT-NewSkill-11: block_trades - 大宗交易统计")
    print("=" * 60)
    from tools.skills.block_trades.main import BlockTradesSkill
    skill = BlockTradesSkill(logger, None)
    result = skill.get_block_trade_stats(10)
    print(f"  类型: {type(result)}")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    assert isinstance(result, list), f"应返回列表: {type(result)}"
    print(f"  统计天数: {len(result)}")
    print(f"  [OK] 大宗交易统计获取成功")


if __name__ == "__main__":
    tests = [
        test_money_flow_stock_fund_flow,
        test_money_flow_sector_fund_flow,
        test_money_flow_north_fund,
        test_margin_trading_summary,
        test_margin_trading_detail,
        test_sector_rotation_ranking,
        test_sector_rotation_history,
        test_sector_rotation_fund_flow,
        test_risk_metrics,
        test_block_trades_detail,
        test_block_trades_stats,
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
        print(f"[PASS] 全部 {passed} 个集成测试通过!")
    else:
        print(f"[RESULT] {passed} passed, {failed} failed out of {len(tests)}")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
