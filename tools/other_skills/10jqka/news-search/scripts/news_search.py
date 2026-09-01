#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻搜索技能主文件
财经领域资讯搜索引擎，调用同花顺问财的财经资讯搜索接口

本技能遵守问财OpenAPI网关规范：
1. 所有请求必须包含X-Claw-* HTTP头
2. 使用Bearer Token认证，从IWENCAI_API_KEY环境变量读取
3. 每次请求生成64字符唯一Trace ID
4. 支持跨平台使用
"""

import os
import sys
import json
import logging
import argparse
import secrets
from typing import List, Dict, Any, Optional
from datetime import datetime
import csv
import time
import requests
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class NewsSearchAPI:
    """同花顺问财经资讯搜索API封装类"""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        初始化API客户端
        
        Args:
            api_key: API密钥，如果为None则从环境变量获取
        """
        self.base_url = "https://openapi.iwencai.com"
        self.endpoint = "/v1/comprehensive/search"
        self.api_key = api_key or os.getenv("IWENCAI_API_KEY")
        
        if not self.api_key:
            raise ValueError("API密钥未设置。请设置环境变量 IWENCAI_API_KEY 或通过参数传入")
        
        self.session = requests.Session()
        
        # 基础请求头
        base_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "NewsSearchSkill/1.0.0"
        }
        
        self.session.headers.update(base_headers)
        
        logger.info("NewsSearchAPI客户端初始化完成")
    
    def _generate_trace_id(self) -> str:
        """
        生成64字符的唯一Trace ID
        
        Returns:
            64字符的十六进制字符串
        """
        return secrets.token_hex(32)
    
    def _get_headers(self, call_type: str = "normal") -> Dict[str, str]:
        """
        获取问财OpenAPI网关要求的HTTP头
        
        Args:
            call_type: 调用类型，normal或retry
            
        Returns:
            包含问财OpenAPI网关要求的HTTP头的字典
        """
        headers = self.session.headers.copy()
        
        # 添加问财OpenAPI网关要求的HTTP头
        headers.update({
            "X-Claw-Call-Type": call_type,
            "X-Claw-Skill-Id": "news-search",
            "X-Claw-Skill-Version": "1.0.0",
            "X-Claw-Plugin-Id": "none",
            "X-Claw-Plugin-Version": "none",
            "X-Claw-Trace-Id": self._generate_trace_id()
        })
        
        return headers
    
    def search_raw(self, query: str, max_retries: int = 3, call_type: str = "normal") -> Dict[str, Any]:
        """
        搜索财经资讯（原始响应，符合问财OpenAPI网关透明传递要求）
        
        Args:
            query: 搜索关键词
            max_retries: 最大重试次数
            call_type: 调用类型，normal或retry
            
        Returns:
            API原始响应JSON，不做任何处理
            
        Note:
            根据问财OpenAPI网关规范条件六，此方法必须透明传递所有API响应，
            包括错误响应，不做任何修改、过滤或重组。
        """
        payload = {
            "channels": ["news"],
            "app_id": "AIME_SKILL",
            "query": query
        }
        
        url = f"{self.base_url}{self.endpoint}"
        
        for attempt in range(max_retries):
            try:
                logger.info(f"搜索查询: {query} (尝试 {attempt + 1}/{max_retries})")
                
                # 获取问财OpenAPI网关要求的HTTP头
                headers = self._get_headers(call_type)
                
                response = self.session.post(url, json=payload, headers=headers, timeout=30)
                
                # 完全透明传递：尝试返回JSON响应，如果不是JSON则返回文本
                try:
                    return response.json()
                except ValueError:
                    # API返回的不是JSON格式，返回原始文本
                    logger.warning(f"API返回非JSON响应，状态码: {response.status_code}")
                    return {
                        "error": "invalid_json_response",
                        "raw_response": response.text,
                        "status_code": response.status_code,
                        "headers": dict(response.headers)
                    }
                    
            except requests.exceptions.Timeout:
                logger.warning(f"请求超时 (尝试 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
                else:
                    # 网络错误：返回错误信息，但仍保持透明结构
                    raise requests.exceptions.Timeout(f"请求超时，已达到最大重试次数: {max_retries}")
            except requests.exceptions.ConnectionError:
                logger.warning(f"连接错误 (尝试 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
                else:
                    # 网络错误：抛出异常，由调用方处理
                    raise requests.exceptions.ConnectionError(f"连接错误，已达到最大重试次数: {max_retries}")
            except Exception as e:
                logger.error(f"搜索过程中发生错误: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
                else:
                    raise
        
        # 不应该执行到这里，但如果执行到了，抛出异常
        raise Exception(f"搜索失败，已达到最大重试次数: {max_retries}")
    
    def search(self, query: str, max_retries: int = 3, call_type: str = "normal") -> Dict[str, Any]:
        """
        搜索财经资讯（透明传递包装方法）
        
        Args:
            query: 搜索关键词
            max_retries: 最大重试次数
            call_type: 调用类型，normal或retry
            
        Returns:
            API原始响应JSON，完全透明传递，不做任何修改
            
        Note:
            根据问财OpenAPI网关规范条件六，此方法完全透明传递API响应，
            调用方需要自己处理响应数据。这是推荐的使用方式。
        """
        return self.search_raw(query, max_retries, call_type)
    
    
    
    def batch_search(self, queries: List[str], delay: float = 1.0, call_type: str = "normal") -> Dict[str, Dict[str, Any]]:
        """
        批量搜索（透明传递版本）
        
        Args:
            queries: 搜索关键词列表
            delay: 每次请求之间的延迟（秒），避免触发频率限制
            call_type: 调用类型，normal或retry
            
        Returns:
            字典，键为查询词，值为对应的透明传递的API响应
            
        Note:
            此方法完全透明传递API响应，符合问财规范条件六。
            调用方需要自己处理响应数据。
        """
        results = {}
        
        for i, query in enumerate(queries):
            try:
                logger.info(f"批量搜索进度: {i+1}/{len(queries)} - {query}")
                response = self.search(query, call_type=call_type)
                results[query] = response
                
                # 避免触发频率限制
                if i < len(queries) - 1:
                    time.sleep(delay)
                    
            except Exception as e:
                logger.error(f"查询 '{query}' 搜索失败: {str(e)}")
                results[query] = {"error": "search_failed", "message": str(e)}
        
        return results


class NewsProcessor:
    """新闻数据处理类"""
    
    @staticmethod
    def filter_by_date(articles: List[Dict[str, Any]], days: int = 7) -> List[Dict[str, Any]]:
        """
        按时间过滤文章
        
        Args:
            articles: 文章列表
            days: 最近多少天内的文章
            
        Returns:
            过滤后的文章列表
        """
        if not articles:
            return []
        
        cutoff_date = datetime.now().timestamp() - (days * 24 * 60 * 60)
        filtered = []
        
        for article in articles:
            publish_date = article.get("publish_date")
            if not publish_date:
                continue
                
            try:
                # 尝试解析日期字符串
                if isinstance(publish_date, str):
                    # 移除可能的时区信息
                    publish_date = publish_date.split('+')[0].strip()
                    dt = datetime.strptime(publish_date, "%Y-%m-%d %H:%M:%S")
                    article_timestamp = dt.timestamp()
                    
                    if article_timestamp >= cutoff_date:
                        filtered.append(article)
            except (ValueError, TypeError):
                # 如果日期解析失败，保留文章
                filtered.append(article)
        
        logger.info(f"时间过滤: 从 {len(articles)} 篇文章中过滤出 {len(filtered)} 篇最近 {days} 天的文章")
        return filtered
    
    @staticmethod
    def sort_by_date(articles: List[Dict[str, Any]], reverse: bool = True) -> List[Dict[str, Any]]:
        """
        按发布时间排序
        
        Args:
            articles: 文章列表
            reverse: True表示从新到旧，False表示从旧到新
            
        Returns:
            排序后的文章列表
        """
        def get_timestamp(article):
            publish_date = article.get("publish_date")
            if not publish_date:
                return 0
                
            try:
                if isinstance(publish_date, str):
                    publish_date = publish_date.split('+')[0].strip()
                    dt = datetime.strptime(publish_date, "%Y-%m-%d %H:%M:%S")
                    return dt.timestamp()
            except (ValueError, TypeError):
                pass
            return 0
        
        sorted_articles = sorted(articles, key=get_timestamp, reverse=reverse)
        return sorted_articles
    
    @staticmethod
    def limit_results(articles: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """
        限制结果数量
        
        Args:
            articles: 文章列表
            limit: 最大返回数量
            
        Returns:
            限制数量后的文章列表
        """
        if limit <= 0:
            return articles
        return articles[:limit]
    
    @staticmethod
    def extract_key_info(article: Dict[str, Any]) -> Dict[str, Any]:
        """
        提取文章关键信息
        
        Args:
            article: 文章字典
            
        Returns:
            包含关键信息的字典
        """
        return {
            "title": article.get("title", ""),
            "summary": article.get("summary", ""),
            "url": article.get("url", ""),
            "publish_date": article.get("publish_date", ""),
            "source": "同花顺问财"
        }
    
    @staticmethod
    def save_to_csv(articles: List[Dict[str, Any]], filepath: str) -> None:
        """
        保存文章到CSV文件
        
        Args:
            articles: 文章列表
            filepath: CSV文件路径
        """
        if not articles:
            logger.warning("没有文章数据可保存到CSV")
            return
        
        # 提取关键信息
        processed_articles = [NewsProcessor.extract_key_info(article) for article in articles]
        
        # 确保目录存在
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
            if processed_articles:
                fieldnames = list(processed_articles[0].keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(processed_articles)
        
        logger.info(f"已保存 {len(processed_articles)} 篇文章到 {filepath}")
    
    @staticmethod
    def save_to_json(articles: List[Dict[str, Any]], filepath: str) -> None:
        """
        保存文章到JSON文件
        
        Args:
            articles: 文章列表
            filepath: JSON文件路径
        """
        if not articles:
            logger.warning("没有文章数据可保存到JSON")
            return
        
        # 提取关键信息
        processed_articles = [NewsProcessor.extract_key_info(article) for article in articles]
        
        # 确保目录存在
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(processed_articles, f, ensure_ascii=False, indent=2)
        
        logger.info(f"已保存 {len(processed_articles)} 篇文章到 {filepath}")


class QueryProcessor:
    """查询处理类，负责思维链拆解"""
    
    @staticmethod
    def split_complex_query(query: str) -> List[str]:
        """
        拆解复杂查询
        
        Args:
            query: 原始查询
            
        Returns:
            拆解后的查询列表
        """
        # 常见的连接词
        connectors = ["和", "与", "及", "以及", "还有", "并且", "同时"]
        
        # 检查是否需要拆解
        needs_split = False
        for connector in connectors:
            if connector in query:
                needs_split = True
                break
        
        if not needs_split:
            return [query]
        
        # 简单的拆解逻辑
        sub_queries = []
        current_query = query
        
        for connector in connectors:
            if connector in current_query:
                parts = current_query.split(connector)
                # 处理每个部分
                for part in parts:
                    part = part.strip()
                    if part:
                        # 进一步检查是否还有连接词
                        sub_queries.extend(QueryProcessor.split_complex_query(part))
                break
        
        # 如果没有拆解成功，返回原始查询
        if not sub_queries:
            sub_queries = [query]
        
        # 去重
        unique_queries = []
        for q in sub_queries:
            if q not in unique_queries:
                unique_queries.append(q)
        
        logger.info(f"查询拆解: '{query}' -> {unique_queries}")
        return unique_queries
    
    @staticmethod
    def evaluate_results(articles: List[Dict[str, Any]], min_articles: int = 3) -> bool:
        """
        评估搜索结果是否足够
        
        Args:
            articles: 文章列表
            min_articles: 最小文章数量要求
            
        Returns:
            是否足够
        """
        if len(articles) >= min_articles:
            logger.info(f"搜索结果评估: 足够 ({len(articles)} 篇文章)")
            return True
        else:
            logger.warning(f"搜索结果评估: 不足 (只有 {len(articles)} 篇文章，需要至少 {min_articles} 篇)")
            return False


def main():
    """主函数，处理命令行参数"""
    parser = argparse.ArgumentParser(
        description="财经新闻搜索工具 - 调用同花顺问财的财经资讯搜索接口（遵守问财OpenAPI网关规范）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s -q "人工智能"
  %(prog)s -q "人工智能和芯片行业" -o results.csv -f csv
  %(prog)s -i queries.txt -o output/ -f json
  echo "人工智能" | %(prog)s -f text

问财OpenAPI网关规范要求:
1. 所有请求必须包含X-Claw-* HTTP头（X-Claw-Call-Type, X-Claw-Skill-Id, X-Claw-Skill-Version等）
2. 使用Bearer Token认证，从IWENCAI_API_KEY环境变量读取API密钥
3. 每次请求生成64字符全局唯一Trace ID
4. 返回结果完全透明传递，不对API响应做任何修改、过滤或重组

curl示例（脱敏，请替换占位符）:
  Unix/Linux/macOS:
    export IWENCAI_API_KEY="your_api_key_here"
    curl -X POST https://openapi.iwencai.com/v1/comprehensive/search \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $IWENCAI_API_KEY" \
      -H "X-Claw-Call-Type: normal" \
      -H "X-Claw-Skill-Id: news-search" \
      -H "X-Claw-Skill-Version: 1.0.0" \
      -H "X-Claw-Plugin-Id: none" \
      -H "X-Claw-Plugin-Version: none" \
      -H "X-Claw-Trace-Id: $(openssl rand -hex 32)" \
      -d '{"channels":["news"],"app_id":"AIME_SKILL","query":"人工智能"}'

  Windows PowerShell:
    $env:IWENCAI_API_KEY="your_api_key_here"
    $traceId = -join ((48..57)+(65..70)+(97..102) | Get-Random -Count 64 | ForEach-Object { [char]$_ })
    curl.exe -X POST https://openapi.iwencai.com/v1/comprehensive/search `
      -H "Content-Type: application/json" `
      -H "Authorization: Bearer $env:IWENCAI_API_KEY" `
      -H "X-Claw-Call-Type: normal" `
      -H "X-Claw-Skill-Id: news-search" `
      -H "X-Claw-Skill-Version: 1.0.0" `
      -H "X-Claw-Plugin-Id: none" `
      -H "X-Claw-Plugin-Version: none" `
      -H "X-Claw-Trace-Id: $traceId" `
      -d '{\"channels\":[\"news\"],\"app_id\":\"AIME_SKILL\",\"query\":\"人工智能\"}'

环境变量设置（跨平台）:
  Unix/Linux/macOS (bash/zsh):
    export IWENCAI_API_KEY="your_api_key_here"

  Windows PowerShell:
    $env:IWENCAI_API_KEY="your_api_key_here"

  Windows CMD:
    set IWENCAI_API_KEY=your_api_key_here

获取API密钥:
  1. 访问同花顺i问财SkillHub: https://www.iwencai.com/skillhub
  2. 登录账号
  3. 点击具体技能，在安装方式-Agent用户中查找您的IWENCAI_API_KEY并复制
  4. 按照上述方式设置环境变量

数据来源: 同花顺问财财经资讯搜索
        """
    )
    
    # 输入参数
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "-q", "--query",
        help="搜索关键词，支持中文"
    )
    input_group.add_argument(
        "-i", "--input",
        help="输入文件路径，每行一个查询词（批量处理）"
    )
    
    # 输出参数
    parser.add_argument(
        "-o", "--output",
        help="输出文件路径或目录"
    )
    parser.add_argument(
        "-f", "--format",
        choices=["csv", "json", "text"],
        default="text",
        help="输出格式 (默认: text)"
    )
    
    # 搜索参数
    parser.add_argument(
        "-l", "--limit",
        type=int,
        default=10,
        help="每查询返回的最大文章数量 (默认: 10)"
    )
    parser.add_argument(
        "-d", "--days",
        type=int,
        default=30,
        help="搜索最近多少天内的文章 (默认: 30)"
    )
    parser.add_argument(
        "--api-key",
        help="API密钥，如果不提供则从环境变量 IWENCAI_API_KEY 获取"
    )
    
    # 其他参数
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试模式"
    )
    
    args = parser.parse_args()
    
    # 设置日志级别
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("调试模式已启用")
    
    try:
        # 初始化API客户端
        api = NewsSearchAPI(api_key=args.api_key)
        processor = NewsProcessor()
        query_processor = QueryProcessor()
        
        # 处理输入
        if args.input:
            # 批量处理模式
            with open(args.input, 'r', encoding='utf-8') as f:
                queries = [line.strip() for line in f if line.strip()]
            
            if not queries:
                logger.error("输入文件为空")
                sys.exit(1)
            
            logger.info(f"批量处理 {len(queries)} 个查询")
            all_results = {}
            
            for query in queries:
                # 拆解复杂查询
                sub_queries = query_processor.split_complex_query(query)
                query_results = []
                
                for sub_query in sub_queries:
                    try:
                        # 获取透明传递的API响应
                        response = api.search(sub_query)
                        
                        # 检查是否为错误响应
                        if "error" in response:
                            # 应用层可以选择性展示关键错误信息
                            error_type = response.get("error", "unknown")
                            status_code = response.get("status_code", 0)
                            raw_response = response.get("raw_response", "")
                            
                            # 对于常见错误（如401），只显示raw_response
                            if status_code == 401 and raw_response:
                                print(f"\n查询 '{query}' - 子查询 '{sub_query}' API错误 (状态码: {status_code}):")
                                print(raw_response)
                            else:
                                # 其他错误显示完整信息
                                print(f"\n查询 '{query}' - 子查询 '{sub_query}' API错误响应:")
                                print(json.dumps(response, indent=2, ensure_ascii=False))
                            continue
                        
                        # 从响应中提取文章数据（在应用层处理，不在API层）
                        articles = response.get("data", [])
                        query_results.extend(articles)
                    except Exception as e:
                        logger.error(f"查询 '{sub_query}' 失败: {str(e)}")
                
                all_results[query] = query_results
            
            # 处理输出
            if args.output:
                output_dir = Path(args.output)
                if output_dir.suffix:  # 是文件
                    if args.format == "csv":
                        # 合并所有结果到一个CSV文件
                        all_articles = []
                        for query, articles in all_results.items():
                            for article in articles:
                                article["original_query"] = query
                                all_articles.append(article)
                        
                        processor.save_to_csv(all_articles, str(output_dir))
                    elif args.format == "json":
                        # 保存为JSON
                        output_data = {}
                        for query, articles in all_results.items():
                            output_data[query] = [
                                processor.extract_key_info(article) for article in articles
                            ]
                        
                        output_dir.parent.mkdir(parents=True, exist_ok=True)
                        with open(output_dir, 'w', encoding='utf-8') as f:
                            json.dump(output_data, f, ensure_ascii=False, indent=2)
                        logger.info(f"已保存批量结果到 {output_dir}")
                else:  # 是目录
                    output_dir.mkdir(parents=True, exist_ok=True)
                    for query, articles in all_results.items():
                        # 创建安全的文件名
                        safe_filename = "".join(c for c in query if c.isalnum() or c in (' ', '-', '_')).rstrip()
                        safe_filename = safe_filename[:50]  # 限制文件名长度
                        
                        if args.format == "csv":
                            filepath = output_dir / f"{safe_filename}.csv"
                            processor.save_to_csv(articles, str(filepath))
                        elif args.format == "json":
                            filepath = output_dir / f"{safe_filename}.json"
                            processor.save_to_json(articles, str(filepath))
            
            # 文本输出
            if args.format == "text" or not args.output:
                for query, articles in all_results.items():
                    print(f"\n{'='*60}")
                    print(f"查询: {query}")
                    print(f"{'='*60}")
                    
                    if not articles:
                        print("未找到相关文章")
                        continue
                    
                    # 处理文章
                    articles = processor.filter_by_date(articles, args.days)
                    articles = processor.sort_by_date(articles)
                    articles = processor.limit_results(articles, args.limit)
                    
                    for i, article in enumerate(articles, 1):
                        print(f"\n{i}. {article.get('title', '无标题')}")
                        print(f"   摘要: {article.get('summary', '无摘要')}")
                        print(f"   发布时间: {article.get('publish_date', '未知时间')}")
                        print(f"   链接: {article.get('url', '无链接')}")
                        print(f"   数据来源: 同花顺问财")
        
        else:
            # 单查询模式
            query = args.query
            
            # 检查是否从管道读取
            if not sys.stdin.isatty() and not query:
                query = sys.stdin.read().strip()
            
            if not query:
                logger.error("未提供查询关键词")
                parser.print_help()
                sys.exit(1)
            
            logger.info(f"处理查询: {query}")
            
            # 拆解复杂查询
            sub_queries = query_processor.split_complex_query(query)
            all_articles = []
            
            for sub_query in sub_queries:
                try:
                    # 获取透明传递的API响应
                    response = api.search(sub_query)
                    
                    # 检查是否为错误响应
                    if "error" in response:
                        # 应用层可以选择性展示关键错误信息
                        error_type = response.get("error", "unknown")
                        status_code = response.get("status_code", 0)
                        raw_response = response.get("raw_response", "")
                        
                        # 对于常见错误（如401），只显示raw_response
                        if status_code == 401 and raw_response:
                            print(f"\n{'='*60}")
                            print(f"API错误 (状态码: {status_code}):")
                            print(f"{'='*60}")
                            print(raw_response)
                            print(f"{'='*60}")
                        else:
                            # 其他错误显示完整信息
                            print(f"\n{'='*60}")
                            print(f"API错误响应（透明传递）:")
                            print(f"{'='*60}")
                            print(json.dumps(response, indent=2, ensure_ascii=False))
                            print(f"{'='*60}")
                        continue
                    
                    # 从响应中提取文章数据（在应用层处理，不在API层）
                    articles = response.get("data", [])
                    all_articles.extend(articles)
                    logger.info(f"子查询 '{sub_query}' 找到 {len(articles)} 篇文章")
                except Exception as e:
                    logger.error(f"子查询 '{sub_query}' 失败: {str(e)}")
            
            # 评估结果
            if not query_processor.evaluate_results(all_articles):
                logger.warning("搜索结果可能不足以完整回答问题")
            
            # 处理文章
            all_articles = processor.filter_by_date(all_articles, args.days)
            all_articles = processor.sort_by_date(all_articles)
            all_articles = processor.limit_results(all_articles, args.limit)
            
            # 处理输出
            if args.output:
                if args.format == "csv":
                    processor.save_to_csv(all_articles, args.output)
                elif args.format == "json":
                    processor.save_to_json(all_articles, args.output)
            
            # 文本输出
            if args.format == "text" or not args.output:
                print(f"\n{'='*60}")
                print(f"查询: {query}")
                print(f"找到 {len(all_articles)} 篇文章 (最近 {args.days} 天)")
                print(f"{'='*60}")
                print(f"数据来源: 同花顺问财财经资讯搜索")
                print(f"{'='*60}")
                
                if not all_articles:
                    print("未找到相关文章")
                else:
                    for i, article in enumerate(all_articles, 1):
                        print(f"\n{i}. {article.get('title', '无标题')}")
                        print(f"   摘要: {article.get('summary', '无摘要')}")
                        print(f"   发布时间: {article.get('publish_date', '未知时间')}")
                        print(f"   链接: {article.get('url', '无链接')}")
        
        logger.info("新闻搜索完成")
        
    except ValueError as e:
        logger.error(f"参数错误: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"程序执行错误: {str(e)}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()