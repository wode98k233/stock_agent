"""专业 Agent 定义 — 自动发现所有 SpecializedAgent 子类"""
import importlib
import pkgutil
from pathlib import Path

from agents.group.subagents.base import SpecializedAgent

# 自动发现并注册所有 agent 类
AGENT_CLASSES: dict[str, type[SpecializedAgent]] = {}


def _discover_agents():
    """自动发现当前包下所有 SpecializedAgent 子类"""
    package_dir = Path(__file__).parent
    package_name = __name__

    for _, module_name, _ in pkgutil.iter_modules([str(package_dir)]):
        if module_name.startswith("_") or module_name in ("base", "tools"):
            continue
        try:
            module = importlib.import_module(f"{package_name}.{module_name}")
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type)
                        and issubclass(attr, SpecializedAgent)
                        and attr is not SpecializedAgent
                        and attr.agent_name):
                    AGENT_CLASSES[attr.agent_name] = attr
        except Exception:
            pass


_discover_agents()


def get_agent_instance(agent_name: str) -> SpecializedAgent:
    """获取 agent 实例"""
    cls = AGENT_CLASSES.get(agent_name)
    if not cls:
        raise KeyError(f"未知 agent: {agent_name}，可用: {list(AGENT_CLASSES.keys())}")
    return cls()


def get_all_agent_info() -> list[dict]:
    """获取所有 agent 的信息，供 planner prompt 使用"""
    return [cls().to_dict() for cls in AGENT_CLASSES.values()]
