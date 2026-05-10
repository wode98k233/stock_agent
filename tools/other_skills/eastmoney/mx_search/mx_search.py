#!/usr/bin/env python3
# mx_search - 妙想资讯搜索 skill
# 基于东方财富妙想搜索API提供金融资讯搜索能力
# 默认输出目录: /root/.openclaw/workspace/mx_data/output/

import os
import sys
import json
import re
import requests
from pathlib import Path
from typing import Dict, List, Optional, Any

def safe_filename(text: str, max_len: int = 80) -> str:
    """Convert query string to safe filenameh"""
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", text).strip().replace(" ", "_")
    return (cleaned[:max_len] or "query").strip("._")

class MXSearch:
    """妙想资讯搜索客户端"""
    
    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"
    
    def __init__(self, api_key: Optional[str] = None):
        """
        初始化客户端
        :param api_key: MX API Key，如果不提供则从环境变量 MX_APIKEY 读取
        """
        self.api_key = api_key or os.getenv("MX_APIKEY")
        if not self.api_key:
            raise ValueError(
                "MX_APIKEY 环境变量未设置，请先设置环境变量：\n"
                "export MX_APIKEY=your_api_key_here\n"
                "或者在初始化时传入 api_key 参数"
            )
    
    def search(self, query: str) -> Dict[str, Any]:
        """
        搜索金融资讯
        :param query: 搜索问句
        :return: API 响应结果
        """
        headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key
        }
        data = {
            "query": query
        }
        
        response = requests.post(self.BASE_URL, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def extract_content(result: Dict[str, Any]) -> str:
        """
        提取纯文本内容
        :param result: API 响应结果
        :return: 提取后的纯文本
        """
        def _extract(raw: Any) -> str:
            if not isinstance(raw, dict):
                if isinstance(raw, str):
                    return raw.strip()
                return ""
            
            # Common envelope format
            for wrapper_key in ("data", "result"):
                wrapped = raw.get(wrapper_key)
                if isinstance(wrapped, dict):
                    nested = _extract(wrapped)
                    if nested:
                        return nested
            
            for key in ("llmSearchResponse", "searchResponse", "content", "answer", "summary"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, (list, dict)):
                    return json.dumps(value, ensure_ascii=False, indent=2)
            
            return json.dumps(raw, ensure_ascii=False, indent=2)
        
        return _extract(result)
    
    @staticmethod
    def format_pretty(result: Dict[str, Any], max_items: int = 10, min_authority: str = "L3") -> str:
        """
        格式化结果用于终端显示，增加质量过滤
        :param result: API 响应结果
        :param max_items: 最多返回条数
        :param min_authority: 最低权威等级 (L1 > L2 > L3)
        """
        authority_order = {"L1": 0, "L1-1": 0, "L1-2": 1, "L1-3": 2,
                          "L2": 3, "L2-1": 3, "L2-2": 4,
                          "L3": 5}
        min_rank = authority_order.get(min_authority, 5)

        output = []

        status = result.get("status")
        message = result.get("message", "")
        if status != 0:
            output.append(f"错误: 状态码 {status} - {message}")
            return "\n".join(output)

        data = result.get("data", {})
        inner_data = data.get("data", {})
        search_response = inner_data.get("llmSearchResponse", {})
        items = search_response.get("data", [])

        if not items:
            return "未找到相关资讯"

        filtered_items = []
        for item in items:
            auth = item.get("authorityLevel", "L2")
            auth_rank = authority_order.get(auth, 5)
            if auth_rank > min_rank:
                continue
            if item.get("informationType") == "NOTICE":
                continue
            filtered_items.append(item)

        # 过滤后为空时降级：跳过权威等级过滤，仅保留 NOTICE 过滤
        if not filtered_items:
            filtered_items = [item for item in items if item.get("informationType") != "NOTICE"]

        filtered_items.sort(key=lambda x: authority_order.get(x.get("authorityLevel", "L2"), 5))
        filtered_items = filtered_items[:max_items]

        output.append(f"搜索结果: 共找到 {len(items)} 条相关资讯，过滤后展示 {len(filtered_items)} 条:\n")

        for i, item in enumerate(filtered_items, 1):
            title = item.get("title", "无标题")
            content = item.get("content", "无内容")
            date = item.get("date", "")
            ins_name = item.get("insName", "")
            info_type = item.get("informationType", "")
            rating = item.get("rating", "")
            entity_name = item.get("entityFullName", "")

            type_map = {
                "REPORT": "研报",
                "NEWS": "新闻",
                "ANNOUNCEMENT": "公告"
            }
            type_cn = type_map.get(info_type, info_type)

            output.append(f"--- {i}. {title} ---")
            meta = []
            if entity_name:
                meta.append(f"证券: {entity_name}")
            if ins_name:
                meta.append(f"机构: {ins_name}")
            if date:
                meta.append(f"日期: {date.split()[0]}")
            if type_cn:
                meta.append(f"类型: {type_cn}")
            if rating:
                meta.append(f"评级: {rating}")

            if meta:
                output.append(" | ".join(meta))

            if content:
                output.append("")
                output.append(content)
            output.append("")

        return "\n".join(output)

def main():
    """命令行入口"""
    # 解析参数
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} \"搜索问句\" [输出目录]")
        print(f"默认输出目录: /root/.openclaw/workspace/mx_data/output/")
        print("示例: python mx_search.py \"格力电器最新研报\"")
        sys.exit(1)
    
    # 拼接查询
    if len(sys.argv) >= 3:
        query = " ".join(sys.argv[1:-1])
        output_dir = Path(sys.argv[-1])
    else:
        query = " ".join(sys.argv[1:])
        # 默认输出到固定目录
        output_dir = Path("/root/.openclaw/workspace/mx_data/output")
    
    # 确保输出目录存在
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        mx = MXSearch()
        result = mx.search(query)
        
        # 终端显示格式化结果
        print(mx.format_pretty(result))
        
        # 提取纯文本保存为 .txt 文件
        content = mx.extract_content(result)
        if content.strip():
            filename = output_dir / f"mx_search_{safe_filename(query)}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"\n✅ 纯文本结果已保存到: {filename}")
        
        # 同时保存原始 JSON 结果
        json_filename = output_dir / f"mx_search_{safe_filename(query)}.json"
        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"📄 原始结果已保存到: {json_filename}")
            
    except Exception as e:
        print(f"错误: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
