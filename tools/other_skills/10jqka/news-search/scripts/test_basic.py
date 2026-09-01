#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻搜索技能基本测试
"""

import os
import sys
import json
import tempfile
from pathlib import Path

# 添加当前目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from news_search import NewsSearchAPI, NewsProcessor, QueryProcessor


def test_query_processor():
    """测试查询处理器"""
    print("测试查询处理器...")
    
    processor = QueryProcessor()
    
    # 测试简单查询
    simple_query = "人工智能"
    result = processor.split_complex_query(simple_query)
    print(f"简单查询 '{simple_query}' -> {result}")
    assert result == ["人工智能"], f"期望 ['人工智能']，得到 {result}"
    
    # 测试复杂查询
    complex_query = "人工智能和芯片行业最新动态"
    result = processor.split_complex_query(complex_query)
    print(f"复杂查询 '{complex_query}' -> {result}")
    assert len(result) >= 2, f"期望至少2个子查询，得到 {len(result)}"
    
    # 测试更多连接词
    test_cases = [
        ("A与B", ["A", "B"]),
        ("A及B", ["A", "B"]),
        ("A以及B", ["A", "B"]),
        ("A还有B", ["A", "B"]),
        ("A并且B", ["A", "B"]),
        ("A同时B", ["A", "B"]),
    ]
    
    for query, expected_prefix in test_cases:
        result = processor.split_complex_query(query)
        print(f"连接词测试 '{query}' -> {result}")
        assert len(result) >= 2, f"期望至少2个子查询，得到 {len(result)}"
    
    print("查询处理器测试通过！\n")


def test_news_processor():
    """测试新闻处理器"""
    print("测试新闻处理器...")
    
    processor = NewsProcessor()
    
    # 测试数据 - 使用当前日期附近的日期
    from datetime import datetime, timedelta
    now = datetime.now()
    
    test_articles = [
        {
            "title": "文章1",
            "summary": "摘要1",
            "url": "http://example.com/1",
            "publish_date": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        },
        {
            "title": "文章2",
            "summary": "摘要2",
            "url": "http://example.com/2",
            "publish_date": (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        },
        {
            "title": "文章3",
            "summary": "摘要3",
            "url": "http://example.com/3",
            "publish_date": (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        }
    ]
    
    # 测试时间过滤
    filtered = processor.filter_by_date(test_articles, days=30)
    print(f"时间过滤: 从 {len(test_articles)} 篇文章中过滤出 {len(filtered)} 篇最近30天的文章")
    assert len(filtered) >= 2, f"期望至少2篇文章，得到 {len(filtered)}"
    
    # 测试排序
    sorted_articles = processor.sort_by_date(test_articles, reverse=True)
    print("排序测试: 文章按时间从新到旧排序")
    
    # 测试数量限制
    limited = processor.limit_results(test_articles, 2)
    print(f"数量限制: 限制为2篇文章，得到 {len(limited)} 篇")
    assert len(limited) == 2, f"期望2篇文章，得到 {len(limited)}"
    
    # 测试关键信息提取
    key_info = processor.extract_key_info(test_articles[0])
    print(f"关键信息提取: {key_info}")
    assert "title" in key_info
    assert "source" in key_info
    assert key_info["source"] == "同花顺问财"
    
    # 测试文件保存
    with tempfile.TemporaryDirectory() as tmpdir:
        # 测试CSV保存
        csv_path = Path(tmpdir) / "test.csv"
        processor.save_to_csv(test_articles, str(csv_path))
        print(f"CSV保存测试: 文件已保存到 {csv_path}")
        assert csv_path.exists()
        
        # 测试JSON保存
        json_path = Path(tmpdir) / "test.json"
        processor.save_to_json(test_articles, str(json_path))
        print(f"JSON保存测试: 文件已保存到 {json_path}")
        assert json_path.exists()
        
        # 验证JSON内容
        with open(json_path, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f)
            assert len(loaded_data) == len(test_articles)
            assert loaded_data[0]["source"] == "同花顺问财"
    
    print("新闻处理器测试通过！\n")


def test_api_client_structure():
    """测试API客户端结构（不实际调用API）"""
    print("测试API客户端结构...")
    
    # 测试初始化
    try:
        # 应该抛出错误，因为没有API密钥
        api = NewsSearchAPI()
        print("警告: API客户端初始化应该失败（无API密钥）")
    except ValueError as e:
        print(f"预期错误: {str(e)}")
    
    # 测试带API密钥的初始化
    test_api_key = "test_key_123"
    api = NewsSearchAPI(api_key=test_api_key)
    
    # 检查属性
    assert api.base_url == "https://openapi.iwencai.com"
    assert api.endpoint == "/v1/comprehensive/search"
    assert api.api_key == test_api_key
    
    print("API客户端结构测试通过！\n")


def test_config_module():
    """测试配置模块"""
    print("测试配置模块...")
    
    try:
        from config import Config, get_config
        
        # 测试默认配置
        config = Config()
        
        # 检查默认值
        api_config = config.get_api_config()
        assert api_config["base_url"] == "https://openapi.iwencai.com"
        assert api_config["timeout"] == 30
        
        search_config = config.get_search_config()
        assert search_config["default_limit"] == 10
        assert search_config["default_days"] == 30
        
        print("配置模块测试通过！")
        
    except ImportError as e:
        print(f"配置模块导入失败: {str(e)}")
        print("跳过配置模块测试")
    
    print()


def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("新闻搜索技能测试套件")
    print("=" * 60)
    print()
    
    tests = [
        test_query_processor,
        test_news_processor,
        test_api_client_structure,
        test_config_module,
    ]
    
    passed = 0
    failed = 0
    
    for test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"测试失败: {str(e)}")
            failed += 1
        except Exception as e:
            print(f"测试异常: {type(e).__name__}: {str(e)}")
            failed += 1
    
    print("=" * 60)
    print(f"测试结果: 通过 {passed}，失败 {failed}")
    print("=" * 60)
    
    if failed == 0:
        print("所有测试通过！")
        return 0
    else:
        print(f"{failed} 个测试失败")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())