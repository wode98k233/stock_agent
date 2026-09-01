#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻搜索技能使用示例
"""

import os
import sys
from pathlib import Path

# 添加当前目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from news_search import NewsSearchAPI, NewsProcessor, QueryProcessor


def example_basic_search():
    """基本搜索示例"""
    print("=" * 60)
    print("示例1: 基本搜索")
    print("=" * 60)
    
    # 注意: 实际使用时需要设置 IWENCAI_API_KEY 环境变量
    # export IWENCAI_API_KEY="your_api_key_here"
    
    api_key = os.getenv("IWENCAI_API_KEY")
    if not api_key:
        print("警告: 未设置 IWENCAI_API_KEY 环境变量")
        print("请先运行: export IWENCAI_API_KEY='your_api_key_here'")
        print("使用模拟数据进行演示...")
        
        # 模拟数据演示
        processor = NewsProcessor()
        test_articles = [
            {
                "title": "人工智能助力金融行业数字化转型",
                "summary": "近日，多家金融机构宣布采用人工智能技术优化风控系统...",
                "url": "https://example.com/article/123",
                "publish_date": "2026-04-01 10:30:00"
            },
            {
                "title": "AI芯片市场迎来爆发式增长",
                "summary": "随着人工智能应用的普及，AI芯片市场需求持续攀升...",
                "url": "https://example.com/article/124",
                "publish_date": "2026-03-28 14:20:00"
            }
        ]
        
        print(f"查询: 人工智能")
        print(f"找到 {len(test_articles)} 篇文章")
        print()
        
        for i, article in enumerate(test_articles, 1):
            print(f"{i}. {article['title']}")
            print(f"   摘要: {article['summary']}")
            print(f"   发布时间: {article['publish_date']}")
            print(f"   链接: {article['url']}")
            print(f"   数据来源: 同花顺问财")
            print()
        
        return
    
    # 实际API调用
    try:
        api = NewsSearchAPI(api_key=api_key)
        processor = NewsProcessor()
        
        # 搜索
        query = "人工智能"
        print(f"搜索查询: {query}")
        articles = api.search(query)
        
        # 处理结果
        articles = processor.filter_by_date(articles, days=30)
        articles = processor.sort_by_date(articles)
        articles = processor.limit_results(articles, 5)
        
        print(f"找到 {len(articles)} 篇文章 (最近30天)")
        print()
        
        for i, article in enumerate(articles, 1):
            print(f"{i}. {article.get('title', '无标题')}")
            print(f"   摘要: {article.get('summary', '无摘要')[:100]}...")
            print(f"   发布时间: {article.get('publish_date', '未知时间')}")
            print(f"   链接: {article.get('url', '无链接')}")
            print(f"   数据来源: 同花顺问财")
            print()
            
    except Exception as e:
        print(f"搜索失败: {str(e)}")


def example_complex_query():
    """复杂查询示例"""
    print("\n" + "=" * 60)
    print("示例2: 复杂查询拆解")
    print("=" * 60)
    
    query_processor = QueryProcessor()
    
    test_queries = [
        "人工智能和芯片行业",
        "新能源汽车与锂电池技术",
        "央行货币政策以及财政政策",
    ]
    
    for query in test_queries:
        sub_queries = query_processor.split_complex_query(query)
        print(f"原始查询: '{query}'")
        print(f"拆解结果: {sub_queries}")
        print()


def example_data_processing():
    """数据处理示例"""
    print("\n" + "=" * 60)
    print("示例3: 数据处理")
    print("=" * 60)
    
    processor = NewsProcessor()
    
    # 模拟数据
    test_articles = [
        {
            "title": "文章A",
            "summary": "摘要A",
            "url": "http://example.com/a",
            "publish_date": "2026-04-01 10:30:00"
        },
        {
            "title": "文章B",
            "summary": "摘要B",
            "url": "http://example.com/b",
            "publish_date": "2026-03-15 14:20:00"
        },
        {
            "title": "文章C",
            "summary": "摘要C",
            "url": "http://example.com/c",
            "publish_date": "2026-02-01 09:15:00"
        }
    ]
    
    print("原始数据:")
    for article in test_articles:
        print(f"  - {article['title']} ({article['publish_date']})")
    
    # 时间过滤
    filtered = processor.filter_by_date(test_articles, days=60)
    print(f"\n时间过滤 (最近60天): {len(filtered)} 篇文章")
    
    # 排序
    sorted_articles = processor.sort_by_date(filtered, reverse=True)
    print("\n按时间排序 (从新到旧):")
    for article in sorted_articles:
        print(f"  - {article['title']} ({article['publish_date']})")
    
    # 提取关键信息
    print("\n关键信息提取示例:")
    key_info = processor.extract_key_info(test_articles[0])
    for key, value in key_info.items():
        print(f"  {key}: {value}")


def example_file_export():
    """文件导出示例"""
    print("\n" + "=" * 60)
    print("示例4: 文件导出")
    print("=" * 60)
    
    processor = NewsProcessor()
    
    # 模拟数据
    test_articles = [
        {
            "title": "人工智能行业报告",
            "summary": "2026年人工智能行业发展前景分析",
            "url": "http://example.com/ai-report",
            "publish_date": "2026-04-01 10:30:00"
        },
        {
            "title": "芯片技术突破",
            "summary": "国产芯片实现技术突破，性能提升显著",
            "url": "http://example.com/chip-breakthrough",
            "publish_date": "2026-03-28 14:20:00"
        }
    ]
    
    import tempfile
    import json
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # CSV导出
        csv_path = tmpdir / "news.csv"
        processor.save_to_csv(test_articles, str(csv_path))
        print(f"CSV文件已保存: {csv_path}")
        
        # JSON导出
        json_path = tmpdir / "news.json"
        processor.save_to_json(test_articles, str(json_path))
        print(f"JSON文件已保存: {json_path}")
        
        # 验证JSON内容
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            print(f"\nJSON文件内容验证:")
            print(f"  文章数量: {len(data)}")
            print(f"  第一篇文章标题: {data[0]['title']}")
            print(f"  数据来源: {data[0]['source']}")


def example_cli_usage():
    """CLI使用示例"""
    print("\n" + "=" * 60)
    print("示例5: CLI命令行使用")
    print("=" * 60)
    
    print("""
基本用法:
  python news_search.py -q "人工智能"
  
搜索最近7天的新闻:
  python news_search.py -q "芯片行业" -d 7
  
限制返回结果数量:
  python news_search.py -q "新能源汽车" -l 5
  
导出为CSV格式:
  python news_search.py -q "人工智能" -o results.csv -f csv
  
导出为JSON格式:
  python news_search.py -q "人工智能" -o results.json -f json
  
批量处理:
  python news_search.py -i queries.txt -o output/ -f csv
  
管道操作:
  echo "人工智能" | python news_search.py
  
启用调试模式:
  python news_search.py -q "测试" --debug
    """)


def main():
    """主函数"""
    print("新闻搜索技能使用示例")
    print("=" * 60)
    print()
    
    examples = [
        example_basic_search,
        example_complex_query,
        example_data_processing,
        example_file_export,
        example_cli_usage,
    ]
    
    for example_func in examples:
        example_func()
        input("\n按Enter键继续...")
        print()
    
    print("=" * 60)
    print("示例演示完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()