"""
仪表盘场景配置 — JSON 配置驱动

从 agents/report_templates/dashboards/ 加载仪表盘定义和关联关系。
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_DASHBOARDS_DIR = Path(__file__).resolve().parent.parent.parent / "agents" / "report_templates" / "dashboards"

# 简单缓存：文件修改时间 → 解析结果
_cache: dict = {}

def _load_json(filename: str):
    """加载 JSON 文件，带 mtime 缓存"""
    filepath = _DASHBOARDS_DIR / filename
    if not filepath.exists():
        return None
    mtime = os.path.getmtime(filepath)
    cache_key = f"{filename}:{mtime}"
    if cache_key in _cache:
        return _cache[cache_key]
    data = json.loads(filepath.read_text(encoding="utf-8"))
    _cache[cache_key] = data
    return data


@dataclass
class DashboardCategory:
    """仪表盘类别配置（保持向后兼容）"""
    extended_fields: list[str]
    scenario_tag: str
    checklist_dimensions: list[str]
    dashboard_id: str = "stock"


def get_dashboard_def(dashboard_id: str) -> dict | None:
    """获取单个仪表盘的完整定义 JSON。"""
    data = _load_json(f"{dashboard_id}.json")
    return data


def list_dashboards() -> list[str]:
    """列出所有注册的仪表盘 ID。"""
    index = _load_json("index.json")
    if not index:
        return ["stock", "market", "sector", "screening"]
    return index.get("dashboards", ["stock"])


def get_associations() -> dict | None:
    """获取模板 ↔ 仪表盘关联映射。"""
    return _load_json("associations.json")


def get_dashboard_category(template_id: str, template: dict = None) -> DashboardCategory | None:
    """根据 template_id 获取仪表盘类别配置。

    优先级:
    1. associations.json 中的映射
    2. template 参数的 dashboard_type 字段
    3. 默认 "stock"
    """
    # 1. 从 associations.json 查找
    assoc = get_associations()
    if assoc:
        for m in assoc.get("mappings", []):
            if m.get("template_id") == template_id:
                dashboard_id = m.get("dashboard_id", "stock")
                return _category_from_def(dashboard_id)

    # 2. 从 template 参数读取 dashboard_type
    if template and template.get("dashboard_type"):
        return _category_from_def(template["dashboard_type"])

    # 3. 默认 stock
    return _category_from_def("stock")


def _category_from_def(dashboard_id: str) -> DashboardCategory | None:
    """从 JSON 定义构建 DashboardCategory 对象。"""
    # 优先加载 JSON 配置
    defn = get_dashboard_def(dashboard_id)
    if defn:
        return DashboardCategory(
            extended_fields=[f["name"] for f in defn.get("llm_fields", [])],
            scenario_tag=defn.get("scenario_tag", "决策仪表盘"),
            checklist_dimensions=defn.get("checklist_dimensions", []),
            dashboard_id=dashboard_id,
        )

    # 降级：硬编码默认
    _FALLBACK: dict[str, DashboardCategory] = {
        "stock": DashboardCategory(
            extended_fields=["price_levels", "split_advice", "position_guidance", "falsification_signal"],
            scenario_tag="个股决策仪表盘",
            checklist_dimensions=["技术面", "基本面", "资金面", "情绪面"],
            dashboard_id="stock",
        ),
        "market": DashboardCategory(
            extended_fields=["market_temperature", "sector_rotation", "sector_stage", "portfolio_action",
                             "action_items", "event_impact", "index_data", "market_breadth",
                             "strong_sectors", "weak_sectors", "volume_summary"],
            scenario_tag="市场决策仪表盘",
            checklist_dimensions=["大盘趋势", "资金流向", "市场情绪", "板块轮动"],
            dashboard_id="market",
        ),
        "sector": DashboardCategory(
            extended_fields=["market_temperature", "sector_stage", "leading_stocks", "portfolio_action", "action_items"],
            scenario_tag="板块决策仪表盘",
            checklist_dimensions=["板块趋势", "龙头表现", "资金流向", "板块情绪"],
            dashboard_id="sector",
        ),
        "screening": DashboardCategory(
            extended_fields=["candidate_stocks", "portfolio_action", "action_items", "falsification_signal"],
            scenario_tag="筛选决策仪表盘",
            checklist_dimensions=["估值面", "基本面", "技术面", "资金面"],
            dashboard_id="screening",
        ),
        "hot_events": DashboardCategory(
            extended_fields=["event_impact", "lifecycle_stage", "sentiment_heat", "capital_behavior",
                             "hot_stocks", "portfolio_action", "action_items"],
            scenario_tag="热点事件仪表盘",
            checklist_dimensions=["事件影响力", "情绪热度", "资金行为", "关联标的"],
            dashboard_id="hot_events",
        ),
    }
    return _FALLBACK.get(dashboard_id)


# 向后兼容：旧调用点使用 get_dashboard_config
get_dashboard_config = get_dashboard_category


def __getattr__(name):
    """模块级 __getattr__，支持 DASHBOARD_CATEGORIES 向后兼容访问。"""
    if name == "DASHBOARD_CATEGORIES":
        result = {}
        for did in list_dashboards():
            cat = _category_from_def(did)
            if cat:
                result[did] = cat
        return result
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
