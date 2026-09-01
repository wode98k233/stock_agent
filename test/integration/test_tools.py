#!/usr/bin/env python3
"""
测试 tools.skills 中的工具
不需要连接 AI，只测试工具功能
"""
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def registry():
    """创建 SkillRegistry 实例"""
    from utils.logger import get_logger
    from utils.memory import MemoryManager
    from tools.skills import SkillRegistry
    from tools.skill_register import SkillRegister

    logger, _, _, _ = get_logger("test_tools")
    memory = MemoryManager(logger)
    sr = SkillRegister()
    sr.auto_discover()
    return SkillRegistry(logger, memory, sr)


def test_stock_query_tools(registry):
    """测试 stock_query 技能的工具"""
    print("\n" + "=" * 60)
    print("测试 stock_query 工具")
    print("=" * 60)
    
    tools = registry.get_tools("stock_query")
    print(f"\n加载工具: {[t.name for t in tools]}")
    
    # 测试 get_board_stocks
    print("\n1. 测试 get_board_stocks('电池')")
    try:
        tool = next(t for t in tools if t.name == "get_board_stocks")
        result = tool.invoke("电池")
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
    
    # 测试 get_stock_realtime
    print("\n2. 测试 get_stock_realtime('600519')")
    try:
        tool = next(t for t in tools if t.name == "get_stock_realtime")
        result = tool.invoke("600519")
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
    
    # 测试 get_stock_history
    print("\n3. 测试 get_stock_history('600519', 30)")
    try:
        tool = next(t for t in tools if t.name == "get_stock_history")
        result = tool.invoke({"symbol": "600519", "days": 30})
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
    
    # 测试 get_stock_rating
    print("\n4. 测试 get_stock_rating('600519')")
    try:
        tool = next(t for t in tools if t.name == "get_stock_rating")
        result = tool.invoke("600519")
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
    
    # 测试 get_stock_financial
    print("\n5. 测试 get_stock_financial('600519')")
    try:
        tool = next(t for t in tools if t.name == "get_stock_financial")
        result = tool.invoke("600519")
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}")


def test_technical_analysis_tools(registry):
    """测试 technical_analysis 技能的工具"""
    print("\n" + "=" * 60)
    print("测试 technical_analysis 工具")
    print("=" * 60)
    
    tools = registry.get_tools("technical_analysis")
    print(f"\n加载工具: {[t.name for t in tools]}")
    
    # 测试 calc_technical_indicators
    print("\n1. 测试 calc_technical_indicators('600519')")
    try:
        tool = next(t for t in tools if t.name == "calc_technical_indicators")
        result = tool.invoke("600519")
        print(f"   结果: {result}...")
        print("   ✅ 通过")
        
        # 保存结果用于后续测试
        indicators_json = result
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return
    
    # 测试 get_tech_summary
    print("\n2. 测试 get_tech_summary(indicators_json)")
    try:
        tool = next(t for t in tools if t.name == "get_tech_summary")
        result = tool.invoke(indicators_json)
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}")


def test_sentiment_analysis_tools(registry):
    """测试 sentiment_analysis 技能的工具"""
    print("\n" + "=" * 60)
    print("测试 sentiment_analysis 工具")
    print("=" * 60)
    
    tools = registry.get_tools("sentiment_analysis")
    print(f"\n加载工具: {[t.name for t in tools]}")
    
    # 测试 search_news_by_keyword
    print("\n1. 测试 search_news_by_keyword('茅台', '600519')")
    try:
        tool = next(t for t in tools if t.name == "search_news_by_keyword")
        result = tool.invoke({"keyword": "茅台", "symbol": "600519"})
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
    
    # 测试 analyze_news_sentiment
    """ print("\n2. 测试 analyze_news_sentiment('600519')")
    try:
        tool = next(t for t in tools if t.name == "analyze_news_sentiment")
        result = tool.invoke("600519")
        print(f"   结果: {result}...")
        print("   ✅ 通过")
    except Exception as e:
        print(f"   ❌ 失败: {e}") """


def test_aggregation_tools(registry):
    """测试 aggregation 技能的工具（需要先获取一些数据）"""
    print("\n" + "=" * 60)
    print("测试 aggregation 工具")
    print("=" * 60)
    
    tools = registry.get_tools("aggregation")
    print(f"\n加载工具: {[t.name for t in tools]}")
    
    # 先获取一些数据用于测试
    print("\n准备测试数据...")
    
    # 选几个股票来测试
    test_symbols = ["600519", "000001", "600036"]  # 贵州茅台、平安银行、招商银行
    indicators_list = []
    
    try:
        tech_tools = registry.get_tools("technical_analysis")
        calc_tool = next(t for t in tech_tools if t.name == "calc_technical_indicators")
        
        for symbol in test_symbols:
            try:
                print(f"   计算 {symbol} 的技术指标...")
                ind_json = calc_tool.invoke(symbol)
                if not ind_json.startswith("错误"):
                    ind_dict = json.loads(ind_json)
                    indicators_list.append(ind_dict)
                    print(f"      ✅ {symbol} 完成")
                else:
                    print(f"      ❌ {symbol} 失败: {ind_json}")
            except Exception as e:
                print(f"      ❌ {symbol} 失败: {e}")
                continue
        
        print(f"   成功获取 {len(indicators_list)} 只股票的技术指标")
    except Exception as e:
        print(f"   ⚠️ 获取测试数据失败: {e}")
        print("   跳过 aggregation 工具测试")
        import traceback
        traceback.print_exc()
        return
    
    if len(indicators_list) == 0:
        print("   没有可用的测试数据，跳过 aggregation 工具测试")
        return
    
    # 测试 stocks_overview
    print("\n1. 测试 stocks_overview")
    try:
        tool = next(t for t in tools if t.name == "stocks_overview")
        result = tool.invoke(json.dumps(indicators_list, ensure_ascii=False))
        print(f"   结果: {result}")
        print("   [OK] 通过")
    except Exception as e:
        print(f"   [FAIL] 失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 其他需要 LLM 的工具暂时跳过，因为需要 AI 连接
    print("\n其他 aggregation 工具需要 LLM，暂时跳过")


def test_data_source_consistency(registry):
    """测试数据源返回格式一致性"""
    print("\n" + "=" * 60)
    print("测试数据源返回格式一致性")
    print("=" * 60)

    test_cases = [
        ("stock_query", "get_board_stocks", {"board_name": "电池"}),
        ("stock_query", "get_stock_realtime", {"symbol": "600519"}),
        ("stock_query", "get_stock_history", {"symbol": "600519", "days": 30}),
        ("technical_analysis", "calc_technical_indicators", {"symbol": "600519"}),
    ]

    for skill_name, tool_name, param in test_cases:
        print(f"\n测试 {skill_name}.{tool_name}")
        try:
            tools = registry.get_tools(skill_name)
            tool = next((t for t in tools if t.name == tool_name), None)
            if not tool:
                print(f"   ⚠️ 工具不存在")
                continue

            result1 = tool.invoke(param)
            result2 = tool.invoke(param)

            if isinstance(result1, str) and isinstance(result2, str):
                print(f"   ✅ 返回类型一致 (str)")
                if result1.startswith("错误:") or result2.startswith("错误:"):
                    print(f"   ⚠️ 返回了错误信息，可能是数据源问题")
            elif isinstance(result1, dict) and isinstance(result2, dict):
                print(f"   ✅ 返回类型一致 (dict)")
            else:
                print(f"   ⚠️ 返回类型不一致: {type(result1)} vs {type(result2)}")
        except Exception as e:
            print(f"   ❌ 异常: {e}")


def main():
    """主测试函数"""
    print("\n" + "═" * 60)
    print("选股雷达 - 工具测试")
    print("═" * 60)
    
    # 创建 logger 和 memory
    logger, _, ctx, _ = get_logger("test_tools")
    memory = MemoryManager(logger)
    
    # 创建技能注册表
    from tools.skill_register import SkillRegister
    skill_register = SkillRegister()
    skill_register.auto_discover()
    registry = SkillRegistry(logger, memory, skill_register)
    
    # 显示技能目录
    # print("\n技能目录:")
    # print(registry.get_catalog_text())
    # print("\n测试 mx_data 技能的详细描述:")
    # print(registry.get_skill_meta("mx_data"))
    # print("\n测试 mx_data 工具:")
    # print(registry.get_skill_tools("mx_data"))
    # print("\n测试 stock_query 技能的详细描述:")
    # print(registry.get_skill_meta("stock_query"))
    # print("\n测试 stock_query 工具:")
    # print(registry.get_skill_tools("stock_query"))
    
    # 测试各个技能
    test_stock_query_tools(registry)
    test_technical_analysis_tools(registry)
    test_sentiment_analysis_tools(registry)
    test_aggregation_tools(registry)
    test_data_source_consistency(registry)
    
    print("\n" + "═" * 60)
    print("测试完成！")
    print("═" * 60)


if __name__ == "__main__":
    main()
