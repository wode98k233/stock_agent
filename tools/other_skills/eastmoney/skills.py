"""
东方财富 Skills 集成
约定标准接口，供 tools.skills.py 自动发现和加载

约定接口：
- get_skill_catalog() -> str: 返回技能目录文本
- get_skill_loaders() -> dict: 返回技能加载器字典 {skill_name: build_func}
- build_all_tools(logger, memory_mgr) -> list: 构建所有技能的工具（兼容）

同时提供 TOOL_REGISTRY 供 SkillRegister 参数校验使用
"""
import os
import sys
import json
import logging

from config import Config
import importlib.util
import yaml
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from utils.app_paths import get_external_skills_dir

logger = logging.getLogger("radar.eastmoney")

EASTMONEY_SKILLS_ROOT = os.path.join(get_external_skills_dir(), "eastmoney")

_eastmoney_skill_meta = {}
_eastmoney_skill_loaders = {}
_eastmoney_catalog = ""


class MXDataQueryParams(BaseModel):
    query: str = Field(..., description="自然语言查询问句，如'贵州茅台最新价 涨跌幅'")


class MXSearchNewsParams(BaseModel):
    query: str = Field(..., description="自然语言搜索问句，如'贵州茅台最新研报'")


class MXXuanguFilterParams(BaseModel):
    query: str = Field(..., description="自然语言选股条件，如'今日涨幅大于2%的A股'")


class MXMoniOperationParams(BaseModel):
    query: str = Field(..., description="自然语言操作指令，如'我的持仓'、'买入 600519 价格 1700 数量 100 股'")


class MXZixuanManageParams(BaseModel):
    query: str = Field(..., description="自然语言管理指令，如'查询我的自选股列表'、'把贵州茅台添加到我的自选股列表'")


def _filter_stocks_data(rows: list, necessary_fields: list) -> list:
    """过滤股票数据，只保留必要字段"""
    if not rows:
        return rows
    if not necessary_fields:
        return rows
    return [
        {k: v for k, v in row.items() if k in necessary_fields}
        for row in rows
    ]


def _mx_data_query_core(query: str) -> str:
    try:
        from .mx_data import MXData
        mx = MXData()
        result = mx.query(query)
        tables, condition_parts, total_rows, err = mx.parse_result(result)
        if err:
            return json.dumps({"query": query, "error": err, "status": "failed"}, ensure_ascii=False)
        terminal_output = mx.format_terminal(result, tables, total_rows)
        structured_tables = [
            {
                "sheet_name": table.get("sheet_name", ""),
                "fieldnames": table.get("fieldnames", []),
                "rows": table.get("rows", [])[:20],
            }
            for table in tables
            if isinstance(table, dict)
        ]
        return json.dumps({
            "query": query,
            "tables_count": len(tables),
            "total_rows": total_rows,
            "tables": structured_tables,
            "terminal_output": terminal_output,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"query": query, "error": str(e), "status": "failed"}, ensure_ascii=False)


def _mx_search_news_core(query: str) -> str:
    try:
        from .mx_search import MXSearch
        mx = MXSearch()
        result = mx.search(query)
        pretty_output = mx.format_pretty(result)
        MAX_OUTPUT_CHARS = 3000
        truncated = False
        if len(pretty_output) > MAX_OUTPUT_CHARS:
            lines = pretty_output.split('\n')
            result_lines = []
            current_len = 0
            item_count = 0
            for line in lines:
                if current_len + len(line) > MAX_OUTPUT_CHARS:
                    truncated = True
                    break
                result_lines.append(line)
                current_len += len(line) + 1
                if line.startswith('--- '):
                    item_count += 1
            pretty_output = '\n'.join(result_lines)
            if truncated:
                pretty_output += f"\n\n... (已截断，仅展示前 {item_count} 条结果)"
        return json.dumps({
            "query": query,
            "result": pretty_output,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"query": query, "error": str(e), "status": "failed"}, ensure_ascii=False)


def _mx_xuangu_filter_core(query: str) -> str:
    try:
        from .mx_xuangu import MXSelectStock
        mx = MXSelectStock()
        result = mx.search(query)
        rows, data_source, err = mx.extract_data(result)
        if err:
            return json.dumps({"query": query, "error": err, "status": "failed"}, ensure_ascii=False)
        filtered_rows = _filter_stocks_data(rows[:20], [])
        return json.dumps({
            "query": query,
            "stock_count": len(filtered_rows),
            "data_source": data_source,
            "stocks": filtered_rows,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"query": query, "error": str(e), "status": "failed"}, ensure_ascii=False)


def _mx_moni_operation_core(query: str) -> str:
    """模拟组合操作 — 委托 mx_moni 子模块实现"""
    from .mx_moni.mx_moni import execute_operation
    result = execute_operation(query)
    return json.dumps(result, ensure_ascii=False)


def _mx_zixuan_manage_core(query: str) -> str:
    """自选股管理 — 委托 mx_zixuan 子模块实现"""
    from .mx_zixuan.mx_zixuan import execute_zixuan_operation
    result = execute_zixuan_operation(query)
    return json.dumps(result, ensure_ascii=False)


TOOL_REGISTRY = {
    "mx_data_query": (_mx_data_query_core, MXDataQueryParams),
    "mx_search_news": (_mx_search_news_core, MXSearchNewsParams),
    "mx_xuangu_filter": (_mx_xuangu_filter_core, MXXuanguFilterParams),
    "mx_moni_operation": (_mx_moni_operation_core, MXMoniOperationParams),
    "mx_zixuan_manage": (_mx_zixuan_manage_core, MXZixuanManageParams),
}

TOOL_DESC_MAP = {
    "mx_data_query": "妙想金融数据查询：查询行情、财务、关联关系等金融数据。输入自然语言查询。",
    "mx_search_news": "妙想资讯搜索：搜索金融相关资讯（新闻、研报、公告等）。输入自然语言查询。",
    "mx_xuangu_filter": "妙想智能选股：根据自然语言条件筛选股票。输入自然语言选股条件。",
    "mx_moni_operation": "妙想模拟组合管理：查询持仓、买卖操作、撤单、委托查询等。输入自然语言指令。",
    "mx_zixuan_manage": "妙想自选股管理：查询、添加、删除自选股。输入自然语言指令。",
}


def _load_eastmoney_single_skill(skill_dir):
    """加载单个东财技能"""
    skill_name = os.path.basename(skill_dir)
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(skill_md_path):
        return
    try:
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
        if md_content.startswith('---'):
            parts = md_content.split('---', 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1].strip())
                content = parts[2].strip()
            else:
                meta = {"name": skill_name, "description": ""}
                content = md_content
        else:
            meta = {"name": skill_name, "description": ""}
            content = md_content
        actual_skill_name = meta.get("name", skill_name).replace("-", "_")

        tool_name_map = {
            "mx_data": "mx_data_query",
            "mx_search": "mx_search_news",
            "mx_xuangu": "mx_xuangu_filter",
            "mx_moni": "mx_moni_operation",
            "mx_zixuan": "mx_zixuan_manage",
        }

        def build_func(logger_obj, memory_mgr):
            tools = []
            tool_name = tool_name_map.get(actual_skill_name)
            if tool_name and tool_name in TOOL_REGISTRY:
                core_func, _ = TOOL_REGISTRY[tool_name]
                desc = TOOL_DESC_MAP.get(tool_name, f"调用 {tool_name}")

                def _make_wrapper(fn, name):
                    def wrapper(query: str) -> str:
                        return fn(query)
                    wrapper.__name__ = name
                    wrapper.__qualname__ = name
                    return wrapper

                named_func = _make_wrapper(core_func, tool_name)
                t = tool(description=desc)(named_func)
                tools.append(t)
            return tools

        _eastmoney_skill_meta[actual_skill_name] = meta
        _eastmoney_skill_loaders[actual_skill_name] = build_func

        global _eastmoney_catalog
        lines = content.split('\n')
        tool_section = []
        in_tools = False
        desc = meta.get('description', '').split('，')[0]
        tool_section.append("### " + actual_skill_name + "(" + desc + ")")
        for line in lines:
            if line.startswith('## 可用工具') or line.startswith('## 功能说明') or line.startswith('## 功能列表'):
                in_tools = True
                continue
            if in_tools:
                if line.startswith('## '):
                    break
                if line.strip():
                    tool_section.append(line)
        if len(tool_section) <= 1:
            for i, line in enumerate(lines):
                if line.strip() and not line.startswith('#'):
                    tool_section.append(line.strip())
                    if len(tool_section) >= 5:
                        break
        if tool_section:
            _eastmoney_catalog += "\n" + "\n".join(tool_section)
    except Exception as e:
        logger.error("加载东财技能 " + skill_name + " 失败: " + str(e))


def _discover_eastmoney_skills():
    """自动发现并加载所有东财技能"""
    global _eastmoney_catalog
    _eastmoney_catalog = "## 东方财富增强技能"
    if not os.path.exists(EASTMONEY_SKILLS_ROOT):
        return
    for item in os.listdir(EASTMONEY_SKILLS_ROOT):
        item_path = os.path.join(EASTMONEY_SKILLS_ROOT, item)
        if os.path.isdir(item_path) and not item.startswith('.') and item != '__pycache__':
            _load_eastmoney_single_skill(item_path)


_discover_eastmoney_skills()


def get_skill_catalog():
    return _eastmoney_catalog


def get_skill_loaders():
    return _eastmoney_skill_loaders.copy()


def build_all_tools(logger_obj, memory_mgr):
    tools = []
    if not Config.MX_APIKEY:
        logger_obj.warning("MX_APIKEY 未配置，东方财富 Skills 不可用")
        return tools
    for skill_name, loader in _eastmoney_skill_loaders.items():
        try:
            skill_tools = loader(logger_obj, memory_mgr)
            tools.extend(skill_tools)
        except Exception as e:
            logger_obj.warning("加载东财技能 " + skill_name + " 失败: " + str(e))
    return tools


build_eastmoney_tools = build_all_tools
