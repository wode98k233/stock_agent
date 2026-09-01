"""
同花顺问财 Skills 集成
约定标准接口，供 tools.skill_register 自动发现和加载

参照 eastmoney/skills.py 的模式：
- 读取每个子目录的 SKILL.md 获取元数据和目录文本
- TOOL_REGISTRY 定义工具名 → (core_func, ParamModel) 映射
- get_skill_loaders() 返回 {skill_name: build_func}，build_func 创建 @tool 装饰的 LangChain tool

约定接口：
- get_skill_catalog() -> str
- get_skill_loaders() -> dict
- build_all_tools(logger, memory_mgr) -> list
- TOOL_REGISTRY
"""
import os
import json
import logging
import secrets

from config import Config

import requests
import yaml
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from utils.app_paths import get_external_skills_dir

logger = logging.getLogger("radar.iwencai")

IWENCAI_SKILLS_ROOT = os.path.join(get_external_skills_dir(), "10jqka")

# ── 问财 API 常量 ──────────────────────────────────────────────────────────
_DEFAULT_BASE_URL = "https://openapi.iwencai.com"
_ENDPOINT = "/v1/comprehensive/search"
_ENDPOINT_QUERY2DATA = "/v1/query2data"
_APP_ID = "AIME_SKILL"

# ── Pydantic 参数模型 ─────────────────────────────────────────────────────
class IWCSearchParams(BaseModel):
    query: str = Field(..., description="自然语言查询问句")


# ── 统一 API 调用 ──────────────────────────────────────────────────────────
def _post_iwencai(endpoint: str, payload: dict, query: str, skill_id: str, skill_version: str) -> str:
    """问财 API 公共请求逻辑（comprehensive/search 和 query2data 共用）"""
    api_key = Config.IWENCAI_API_KEY
    if not api_key:
        return json.dumps({"error": "IWENCAI_API_KEY 未配置", "status": "failed"}, ensure_ascii=False)

    base_url = Config.IWENCAI_BASE_URL or _DEFAULT_BASE_URL
    url = f"{base_url}{endpoint}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": skill_id,
        "X-Claw-Skill-Version": skill_version,
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            return json.dumps({
                "error": "invalid_json_response",
                "status_code": resp.status_code,
                "raw": resp.text[:2000],
            }, ensure_ascii=False)

        result = {"query": query, "skill_id": skill_id, "data": data}
        if "channels" in payload:
            result["channels"] = payload["channels"]
        return json.dumps(result, ensure_ascii=False)
    except requests.exceptions.Timeout:
        return json.dumps({"query": query, "error": "请求超时", "status": "failed"}, ensure_ascii=False)
    except requests.exceptions.ConnectionError:
        return json.dumps({"query": query, "error": "连接失败", "status": "failed"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"query": query, "error": str(e), "status": "failed"}, ensure_ascii=False)


def _iwencai_search(query: str, channels: list, skill_id: str, skill_version: str) -> str:
    """comprehensive/search 端点"""
    payload = {"channels": channels, "app_id": _APP_ID, "query": query}
    return _post_iwencai(_ENDPOINT, payload, query, skill_id, skill_version)


def _iwencai_query2data(query: str, skill_id: str, skill_version: str) -> str:
    """query2data 端点（选板块、行情查询等）"""
    payload = {"query": query, "page": "1", "limit": "30", "is_cache": "1", "expand_index": "true"}
    return _post_iwencai(_ENDPOINT_QUERY2DATA, payload, query, skill_id, skill_version)


# ── 各 skill core 函数 ─────────────────────────────────────────────────────
def _iwc_news_search_core(query: str) -> str:
    return _iwencai_search(query, ["news"], "news-search", "1.0.0")


def _iwc_announcement_search_core(query: str) -> str:
    return _iwencai_search(query, ["announcement"], "announcement-search", "1.0.0")


# ── 各 skill core 函数（query2data 端点）──────────────────────────────────
def _iwc_sector_selector_core(query: str) -> str:
    return _iwencai_query2data(query, "hithink-sector-selector", "1.0.0")


def _iwc_market_query_core(query: str) -> str:
    return _iwencai_query2data(query, "hithink-market-query", "1.0.0")


# ── TOOL_REGISTRY ──────────────────────────────────────────────────────────
TOOL_REGISTRY = {
    "iwc_news_search": (_iwc_news_search_core, IWCSearchParams),
    "iwc_announcement_search": (_iwc_announcement_search_core, IWCSearchParams),
    "iwc_sector_selector": (_iwc_sector_selector_core, IWCSearchParams),
    "iwc_market_query": (_iwc_market_query_core, IWCSearchParams),
}


# ── Skill 注册元数据 ───────────────────────────────────────────────────────
_iwc_skill_meta = {}
_iwc_skill_loaders = {}
_iwc_catalog = ""


def _load_iwencai_single_skill(skill_dir):
    """加载单个问财技能（参照 eastmoney/_load_eastmoney_single_skill）"""
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

        # tool_name_map: SKILL 目录名 → TOOL_REGISTRY key
        tool_name_map = {
            "news_search": "iwc_news_search",
            "announcement_search": "iwc_announcement_search",
            "hithink_sector_selector": "iwc_sector_selector",
            "hithink_market_query": "iwc_market_query",
        }

        # 工具描述：简洁版，一句话说明作用、参数、返回
        tool_desc_map = {
            "iwc_news_search": "同花顺问财新闻搜索：搜索财经新闻、政策动态、行业资讯。参数：query（自然语言问句）。返回：新闻标题、摘要、链接、时间。",
            "iwc_announcement_search": "同花顺问财公告搜索：搜索A股/港股/基金公告。参数：query（自然语言问句）。返回：公告标题、摘要、链接、时间。",
            "iwc_sector_selector": '同花顺问财选板块：通过行业估值、资金流向、涨跌幅、板块类型等多条件组合筛选市场板块。参数：query（自然语言问句，如"涨幅前五的板块"、"资金净流入的行业板块"）。返回：板块名称、涨跌幅、主力资金净流入等。',
            "iwc_market_query": '同花顺问财行情查询：获取股票、ETF、指数等实时价格、涨跌幅、成交量、主力资金流向、技术指标等行情数据。参数：query（自然语言问句，如"同花顺最新价格"、"上证指数行情"）。返回：股票代码、简称、最新价、涨跌幅等行情数据。',
        }

        def build_func(logger_obj, memory_mgr):
            tools = []
            tool_name = tool_name_map.get(actual_skill_name)
            if tool_name and tool_name in TOOL_REGISTRY:
                core_func, _ = TOOL_REGISTRY[tool_name]
                desc = tool_desc_map.get(tool_name, f"调用 {tool_name}")

                # 创建带正确名称的包装函数（tool 装饰器用 __name__ 作工具名）
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

        _iwc_skill_meta[actual_skill_name] = meta
        _iwc_skill_loaders[actual_skill_name] = build_func

        # 构建 catalog 文本（用于 skill filter 阶段）
        global _iwc_catalog
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
            _iwc_catalog += "\n" + "\n".join(tool_section)
    except Exception as e:
        logger.error(f"加载问财技能 {skill_name} 失败: {e}")


def _discover_iwencai_skills():
    """自动发现并加载所有问财技能"""
    global _iwc_catalog
    _iwc_catalog = "## 同花顺问财增强技能"
    if not os.path.exists(IWENCAI_SKILLS_ROOT):
        return
    for item in os.listdir(IWENCAI_SKILLS_ROOT):
        item_path = os.path.join(IWENCAI_SKILLS_ROOT, item)
        if os.path.isdir(item_path) and not item.startswith('.') and item != '__pycache__':
            _load_iwencai_single_skill(item_path)


_discover_iwencai_skills()


# ── 标准接口 ───────────────────────────────────────────────────────────────
def get_skill_catalog():
    return _iwc_catalog


def get_skill_loaders():
    return _iwc_skill_loaders.copy()


def build_all_tools(logger_obj, memory_mgr):
    tools = []
    if not Config.IWENCAI_API_KEY:
        logger_obj.warning("IWENCAI_API_KEY 未配置，同花顺问财 Skills 不可用")
        return tools
    for skill_name, loader in _iwc_skill_loaders.items():
        try:
            skill_tools = loader(logger_obj, memory_mgr)
            tools.extend(skill_tools)
        except Exception as e:
            logger_obj.warning(f"加载问财技能 {skill_name} 失败: {e}")
    return tools
